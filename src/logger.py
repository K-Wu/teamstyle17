#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import collections
import gzip
import json
import queue
import time
import threading
import sortedcontainers

import main
import action
import ts17core


class RunLogger(threading.Thread):
    def __init__(self, filename):
        threading.Thread.__init__(self)
        if not filename:
            filename = 'ts17_' + time.strftime('%m%d%H%M%S') + '.rpy'
        self._filename = filename
        self._fp = gzip.open(self._filename, 'wt', encoding='utf-8')
        self.sig = queue.Queue()

    def run(self):
        while 1:
            q = self.sig.get(block=True)
            if q == 0:
                self._fp.close()
                break
            self._fp.write(q)
            self._fp.write('\n')

    def exit(self):
        self.sig.put(0)


class RepGame:
    MAX_DELAY_ROUNDS = 1
    ROUNDS_PER_SEC = 20

    def __init__(self, verbose: bool, info_callback):
        self._timer = main.Timer()
        self._last_action_timestamp = 0
        self._logger = main.Logging(
            timer=lambda: '%d @ %.6f' % (self._last_action_timestamp, self._timer.current_time))
        self._logger.basic_config(level=main.Logging.DEBUG if verbose else main.Logging.INFO)
        self._logic = ts17core.interface.Interface(info_callback)
        self.queue = collections.deque()
        self.sig = queue.Queue()
        self._action_buffer = None

        # RepGame 的 _active 属性用于标识自己当前是否是活动的
        # 非活动的 RepGame 不应该接受任何指令
        # 通过 RepGame.sig.put(0) 来使 RepGame进入非活动状态

    def mainloop(self):
        while 1:
            next_action = None
            if self._action_buffer is None and not self.queue:
                self._timer.stop()
                self._timer.elapsed = self.__real_time(self._last_action_timestamp)
                return
            if self._action_buffer is None:
                self._action_buffer = self.queue.popleft()
            t = self.__timeout_before_round(min(self._action_buffer[0], self._last_action_timestamp + 1))
            if t < 0:
                t = 0
            if not self._timer.running or not self.sig.empty() or t > 0:
                try:
                    q = self.sig.get(block=True, timeout=(t if self._timer.running else None))
                except queue.Empty:
                    q = None
                if q == 0:
                    self.set_round(self._last_action_timestamp + 1)
                    return
                elif q == 1:
                    self._timer.running = not self._timer.running
                    continue
                elif type(q) == action.Action:
                    """
                    平台查询指令 (str, queue)
                    """
                    next_action = (self._last_action_timestamp, q)
                elif type(q) == queue.Queue:
                    self.set_round(self._last_action_timestamp + 1)
                    q.put(0)
                    return
            if next_action is None:
                if self._action_buffer[0] <= self.__logic_time(self.current_time):
                    next_action = self._action_buffer
                    self._action_buffer = None
                else:
                    while self._action_buffer[0] > self._last_action_timestamp and self.__logic_time(
                            self.current_time) > self._last_action_timestamp:
                        try:
                            self._logic.nextTick()
                        except Exception as e:
                            main.root_logger.critical('logic exception %s [%s]', type(e).__name__, str(e))
                            raise
                        self._last_action_timestamp += 1
                    continue
            if next_action[0] < self.__logic_time(self.current_time):
                self._timer.current_time = self.__real_time(next_action[0] + 1)
            elif next_action[0] > self.__logic_time(self.current_time):
                self._action_buffer = next_action
                continue
            j_str = next_action[1].action_json
            if j_str.endswith('\n'):
                j_str = j_str.replace('\n', '')
            self._logger.debug('>>>>>>>> recv %s', j_str or '')
            while next_action[0] > self._last_action_timestamp:
                try:
                    self._logic.nextTick()
                except Exception as e:
                    main.root_logger.critical('logic exception %s [%s]', type(e).__name__, str(e))
                    raise
                self._last_action_timestamp += 1
            next_action[1].set_timestamp(self._last_action_timestamp)
            next_action[1].run(self._logic)
            self._logger.debug('<<<<<<<< fin')

    @property
    def current_time(self) -> float:
        return self._timer.current_time

    @staticmethod
    def __logic_time(timestamp: float) -> int:
        return int(timestamp * RepGame.ROUNDS_PER_SEC)

    @staticmethod
    def __real_time(timestamp: int) -> float:
        return timestamp / RepGame.ROUNDS_PER_SEC

    def __timeout_before_round(self, timestamp: int):
        return timestamp / RepGame.ROUNDS_PER_SEC - self.current_time

    def enqueue(self, _, act):
        if act.action_name == '_pause':
            self.sig.put(1)
        else:
            self.sig.put(act)

    def set_round(self, timestamp: int):
        if timestamp > self._last_action_timestamp:
            self._timer.stop()
            self._timer.elapsed = self.__real_time(timestamp)
            if self._action_buffer is None and self.queue:
                self._action_buffer = self.queue.popleft()
            while self._action_buffer and self._action_buffer[0] < timestamp:
                while self._action_buffer[0] > self._last_action_timestamp:
                    try:
                        self._logic.nextTick()
                    except Exception as e:
                        main.root_logger.critical('logic exception %s [%s]', type(e).__name__, str(e))
                        raise
                    self._last_action_timestamp += 1
                if self._action_buffer[1].action_name == 'game_end':
                    self._timer.elapsed = self.__real_time(self._action_buffer[0])
                    break
                self._action_buffer[1].run(self._logic)
                if self.queue:
                    self._action_buffer = self.queue.popleft()
                else:
                    main.root_logger.error('Unexpected ending of replay file.')
                    break
            while timestamp > self._last_action_timestamp:
                try:
                    self._logic.nextTick()
                except Exception as e:
                    main.root_logger.critical('logic exception %s [%s]', type(e).__name__, str(e))
                    raise
                self._last_action_timestamp += 1


class RepManager:
    def __init__(self, rep_file_name: str, verbose: bool, start_paused: bool):
        self._rep_file = rep_file_name
        self._games = sortedcontainers.SortedDict()
        self._info_callback = lambda _: None
        self._verbose = verbose
        self._active_game = RepGame(verbose=verbose, info_callback=self.__info_callback)
        self._rounds = _load_queue(rep_file_name, self._active_game.queue)
        '''
        dist = self._rounds // 64
        if dist == 0:
            dist = 1
        p_pos = 0
        buf_p = RepGame(verbose=verbose, info_callback=lambda _: None)
        buf_q = RepGame(verbose=verbose, info_callback=lambda _: None)
        copy_rep_game(self._active_game, buf_p)
        while p_pos < self._rounds:
            copy_rep_game(buf_p, buf_q)
            self._games[p_pos] = buf_q
            p_pos += dist
            buf_p.set_round(p_pos)
        '''
        self._active_game.queue.popleft()[1].run(self._active_game._logic)
        self._rep_thread = None
        self.sig = queue.Queue()
        self._ui_running = lambda: False
        self._start_paused = start_paused

    def enqueue(self, timestamp, act):
        if act.action_name == '_set_time':
            self.set_round(timestamp)
        elif act.action_name == '_end':
            self.sig.put(False)
            self._active_game.sig.put(0)
        elif act.action_name == '_query_rounds':

            # 危险的代码
            act.return_queue.put('%d\n' % self._rounds)

        else:
            if self._active_game._action_buffer is None and not self._active_game.queue:
                self._active_game.enqueue(timestamp, act)
                self.sig.put(True)
            else:
                self._active_game.enqueue(timestamp, act)

    def __info_callback(self, obj: str):
        self._info_callback(obj)

    def mainloop(self):
        q = True
        while q:
            if not self._start_paused:
                self._active_game._timer.start()

            self._active_game.mainloop()
            if self._ui_running():
                q = self.sig.get()
            else:
                q = False

    def set_round(self, timestamp):
        # 醉了，需要重新检查
        callback_backup = self._info_callback
        self._info_callback = lambda _: None
        self._active_game._timer.stop()
        if timestamp > self._active_game._last_action_timestamp:
            self._active_game.set_round(timestamp)
        else:
            r = queue.Queue()
            self._active_game.sig.put(r)
            r.get()
            ts = self._active_game._last_action_timestamp
            if ts not in self._games:
                self._games[ts] = self._active_game
            pos = self._games.bisect(timestamp)
            if pos == 0:
                self._active_game = RepGame(verbose=self._verbose, info_callback=self.__info_callback)
                _load_queue(self._rep_file, self._active_game.queue)
                if timestamp:
                    self._active_game.set_round(timestamp)
                else:
                    self._active_game.queue.popleft()[1].run(self._active_game._logic)
            else:
                self._active_game = self._games[self._games.iloc[pos - 1]]
                del self._games.iloc[pos - 1]
                if timestamp > self._active_game._last_action_timestamp:
                    self._active_game.set_round(timestamp)
            self.sig.put(True)
        self._info_callback = callback_backup
        main.root_logger.info('set round fin: %d', self._active_game._last_action_timestamp)

    @property
    def current_time(self) -> float:
        return self._active_game.current_time


def _load_queue(file_name: str, target: collections.deque):
    r = 0
    try:
        with gzip.open(file_name, 'rt', encoding='utf-8') as rep_file:
            for line in rep_file:
                j = json.loads(line)
                t = j.get('time')
                if t is None:
                    t = 0
                k = j.get('action')
                if k != 'game_end':
                    target.append((t, action.Action(line, 'instruction', None)))
                else:
                    r = t
                    target.append((t, action.Action(line, 'game_end', None)))
    except OSError:
        main.root_logger.error('Corrupted replay file: %s', file_name)
        target.append((0, action.Action('{"action":"game_end","ai_id":-2,"time":0}', 'game_end', None)))
    finally:
        return r


'''
def copy_rep_game(src: RepGame, dst: RepGame):
    dst._timer = copy.deepcopy(src._timer)
    dst.queue = copy.deepcopy(src.queue)
    dst._logic = copy.deepcopy(src._logic)
    dst._action_buffer = copy.deepcopy(src._action_buffer)
    dst._last_action_timestamp = src._last_action_timestamp
'''
