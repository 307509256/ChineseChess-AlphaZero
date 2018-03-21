from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from logging import getLogger
from threading import Lock, Condition

import numpy as np

from cchess_alphazero.config import Config
from cchess_alphazero.environment.env import CChessEnv
from cchess_alphazero.environment.lookup_tables import Winner, ActionLabelsRed, flip_policy, flip_move, flip_action_labels
from time import time, sleep

logger = getLogger(__name__)

class VisitState:
    def __init__(self):
        self.a = defaultdict(ActionState)   # key: action, value: ActionState
        self.sum_n = 0                      # visit count
        self.visit = []                     # thread id that has visited this state
        self.p = None                       # policy of this state
        self.legal_moves = None             # all leagal moves of this state
        self.waiting = False                # is waiting for NN's predict


class ActionState:
    def __init__(self):
        self.n = 0      # N(s, a) : visit count
        self.w = 0      # W(s, a) : total action value
        self.q = 0      # Q(s, a) = N / W : action value
        self.p = -1     # P(s, a) : prior probability

class CChessPlayer:
    def __init__(self, config: Config, search_tree=None, pipes=None, play_config=None, enable_resign=False, debugging=False):
        self.moves = []     # store move data
        self.config = config
        self.play_config = play_config or self.config.play
        self.labels_n = len(ActionLabelsRed)
        self.labels = ActionLabelsRed
        self.move_lookup = {move: i for move, i in zip(self.labels, range(self.labels_n))}
        self.pipe = pipes                   # pipes that used to communicate with CChessModelAPI thread
        self.node_lock = defaultdict(Lock)  # key: state key, value: Lock of that state

        if search_tree is None:
            self.tree = defaultdict(VisitState)  # key: state key, value: VisitState
        else:
            self.tree = search_tree

        self.enable_resign = enable_resign
        self.debugging = debugging

        self.search_results = {}        # for debug
        self.debug = {}

        self.s_lock = Lock()
        self.run_lock = Lock()
        self.q_lock = Lock()            # queue lock
        self.t_lock = Lock()
        self.buffer_planes = []         # prediction queue
        self.buffer_history = []

        self.all_done = Lock()
        self.num_task = 0

        self.job_done = False

        self.executor = ThreadPoolExecutor(max_workers=self.play_config.search_threads+2)
        self.executor.submit(self.receiver)
        self.executor.submit(self.sender)
        self.pp = None

    def get_state_key(self, env: CChessEnv) -> str:
        board = env.observation
        board = board.split(' ')
        return board[0]

    def get_legal_moves(self, env: CChessEnv):
        legal_moves = env.board.legal_moves()
        if not env.red_to_move:
            legal_moves = flip_action_labels(legal_moves)
        return legal_moves

    def close(self):
        self.job_done = True
        if self.executor is not None:
            self.executor.shutdown()

    def sender(self):
        '''
        send planes to neural network for prediction
        '''
        limit = 256                 # max prediction queue size
        while not self.job_done:
            self.run_lock.acquire()
            with self.q_lock:
                l = min(limit, len(self.buffer_history))
                if l > 0:
                    t_data = self.buffer_planes[0:l]
                    self.pipe.send(t_data)
                else:
                    self.run_lock.release()
                    sleep(0.001)

    def receiver(self):
        '''
        receive policy and value from neural network
        '''
        while not self.job_done:
            if self.pipe.poll(0.001):
                rets = self.pipe.recv()
            else:
                continue
            k = 0
            with self.q_lock:
                for ret in rets:
                    self.executor.submit(self.update_tree, ret[0], ret[1], self.buffer_history[k])
                    k = k + 1
                self.buffer_planes = self.buffer_planes[k:]
                self.buffer_history = self.buffer_history[k:]
            self.run_lock.release()

    def action(self, env: CChessEnv) -> str:
        value = self.search_moves(env)  # MCTS search
        policy = self.calc_policy(env, env.num_halfmoves)  # policy will not be flipped in `calc_policy`

        if policy is None:  # resign
            return None

        if not env.red_to_move:
            pol = flip_policy(policy)
        else:
            pol = policy
        my_action = int(np.random.choice(range(self.labels_n), p=self.apply_temperature(pol, env.num_halfmoves)))
        # my_action = np.argmax(self.apply_temperature(pol, env.num_halfmoves))
        # no resign
        self.moves.append([env.observation, list(policy)])    # do not need flip anymore when training
        return self.labels[my_action]

    def MCTS_search(self, env: CChessEnv, is_root_node=False, history=[]) -> float:
        """
        Monte Carlo Tree Search
        """
        while True:
            if env.done:
                if env.winner == Winner.draw:
                    v = 0
                elif (env.winner == Winner.red and env.red_to_move) or (env.winner == Winner.black and not env.red_to_move):
                    v = 1
                else:
                    v = -1
                self.executor.submit(self.update_tree, None, v, history)
                break

            state = self.get_state_key(env)

            with self.node_lock[state]:
                if state not in self.tree:
                    # Expand and Evaluate
                    self.tree[state].sum_n = 1
                    self.tree[state].legal_moves = self.get_legal_moves(env)
                    self.expand_and_evaluate(state, history)
                    break

                if state in history[:-1]: # loop -> loss
                    self.executor.submit(self.update_tree, None, 0, history)
                    break

                # Select
                node = self.tree[state]
                if node.waiting:
                    node.visit.append(history)
                    break

                sel_action = self.select_action_q_and_u(state, is_root_node)
                is_root_node = False
                
                if sel_action is None:
                    return -1

                virtual_loss = self.config.play.virtual_loss
                self.tree[state].sum_n += virtual_loss
                
                action_state = self.tree[state].a[sel_action]
                action_state.n += virtual_loss
                action_state.w -= virtual_loss
                action_state.q = action_state.w / action_state.n

        if env.red_to_move:
            env.step(sel_action)
        else:
            env.step(flip_move(sel_action))

        leaf_v = self.MCTS_search(env, False, tid)
        leaf_v = -leaf_v

        # Backup
        # update N, W, Q
        with self.node_lock[state]:
            node = self.tree[state]
            node.visit.remove(tid)
            node.sum_n = node.sum_n - virtual_loss + 1

            action_state = node.a[sel_action]
            action_state.n += 1 - virtual_loss
            action_state.w += leaf_v + virtual_loss
            action_state.q = action_state.w / action_state.n

        return leaf_v

    def select_action_q_and_u(self, state, is_root_node) -> str:
        '''
        Select an action with highest Q(s,a) + U(s,a)
        '''
        node = self.tree[state]
        legal_moves = node.legal_moves

        # push p, the prior probability to the edge (node.p), only consider legal moves
        if node.p is not None:
            all_p = 0
            for mov in legal_moves:
                mov_p = node.p[self.move_lookup[mov]]
                node.a[mov].p = mov_p
                all_p += mov_p
            # rearrange the distribution
            if all_p == 0:
                all_p = 1
            for mov in legal_moves:
                node.a[mov].p /= all_p
            # release the temp policy
            node.p = None

        # sqrt of sum(N(s, b); for all b)
        xx_ = np.sqrt(node.sum_n + 1)  

        e = self.play_config.noise_eps
        c_puct = self.play_config.c_puct
        dir_alpha = self.play_config.dirichlet_alpha

        best_score = -99999999
        best_action = None

        for mov in legal_moves:
            action_state = node.a[mov]
            p_ = action_state.p
            if is_root_node:
                p_ = (1 - e) * p_ + e * np.random.dirichlet([dir_alpha])
            # Q + U
            score = action_state.q + c_puct * p_ * xx_ / (1 + action_state.n)
            if action_state.q > (1 - 1e-7):
                best_action = mov
                break
            if score > best_score:
                best_score = score
                best_action = mov

        if best_action == None:
            logger.error(f"Best action is None, legal_moves = {legal_moves}, best_score = {best_score}")

        return best_action

    def expand_and_evaluate(self, env: CChessEnv) -> (np.ndarray, float):
        '''
        Evaluate the state, return its policy and value computed by neural network
        '''
        state_planes = env.input_planes()
        # communicate with model api
        pipe = self.pipe_pool.pop()
        pipe.send(state_planes)
        leaf_p, leaf_v = pipe.recv()
        self.pipe_pool.append(pipe)
        # leaf_p, leaf_v = (np.random.random(len(ActionLabelsRed)), 1)
        # these are canonical policy and value (i.e. side to move is "red", maybe need flip)
        return leaf_p, leaf_v

    def calc_policy(self, env: CChessEnv, turns) -> np.ndarray:
        '''
        calculate π(a|s0) according to the visit count
        '''
        state = self.get_state_key(env)
        node = self.tree[state]
        policy = np.zeros(self.labels_n)

        max_q_value = -100
        for mov, action_state in node.a.items():
            policy[self.move_lookup[mov]] = action_state.n
            if self.debugging:
                self.search_results[mov] = (action_state.n, action_state.q, action_state.p)
            if action_state.q > max_q_value:
                max_q_value = action_state.q

        if max_q_value < self.play_config.resign_threshold and self.enable_resign and turns > self.play_config.min_resign_turn:
            return None

        policy /= np.sum(policy)
        return policy

    def apply_temperature(self, policy, turn) -> np.ndarray:
        # tau = np.power(self.play_config.tau_decay_rate, turn + 1)
        if turn < 15 and self.play_config.tau_decay_rate != 0:
            tau = 1
        else:
            tau = 0
        # if tau < 0.1:
        #     tau = 0
        if tau == 0:
            action = np.argmax(policy)
            ret = np.zeros(self.labels_n)
            ret[action] = 1.0
            return ret
        else:
            ret = np.power(policy, 1 / tau)
            ret /= np.sum(ret)
            return ret

    def finish_game(self, z):
        """
        :param z: win=1, lose=-1, draw=0
        """
        # add the game winner result to all past moves.
        for move in self.moves:  
            move += [z]

