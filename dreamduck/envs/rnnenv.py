import numpy as np
import json
from pyglet.window import key
import sys
import pyglet
from gym import spaces
from gym.spaces.box import Box
from dreamduck.envs.env import DuckieTownWrapper
from dreamduck.envs.rnn.rnn import reset_graph, rnn_model_path_name,\
    model_rnn_size, model_state_space, MDNRNN, hps_sample
from dreamduck.envs.vae.vae import ConvVAE, vae_model_path_name
import os
from gym.utils import seeding
from scipy.misc import imresize as resize
import tensorflow as tf

# actual observation size
SCREEN_X = 64
SCREEN_Y = 64
TEMPERATURE = 1.25  # train with this temperature

model_path_name = \
    'dreamduck/envs/tf_initial_z'


def _process_frame(frame):
    obs = frame[0:84, :, :].astype(np.float)/255.0
    obs = ((1.0 - obs) * 255).round().astype(np.uint8)
    return obs


def get_pi_idx(x, pdf):
    # samples from a categorial distribution
    N = pdf.size
    accumulate = 0
    for i in range(0, N):
        accumulate += pdf[i]
        if (accumulate >= x):
            return i
    random_value = np.random.randint(N)
    # print('error with sampling ensemble, returning random', random_value)
    return random_value


# Dreaming
class DuckieTownRNN(DuckieTownWrapper):
    metadata = {
        'render.modes': ['human', 'rgb_array'],
        'video.frames_per_second': 50
    }

    def __init__(self, render_mode=False, load_model=True):
        self.render_mode = render_mode

        with open(os.path.join(model_path_name, 'initial_z.json'), 'r') as f:
            [initial_mu, initial_logvar] = json.load(f)
        self.initial_mu_logvar = [list(elem)
                                  for elem in zip(initial_mu, initial_logvar)]

        reset_graph()

        self.vae = ConvVAE(batch_size=1, gpu_mode=False,
                           is_training=False, reuse=True)
        self.rnn = MDNRNN(hps_sample, gpu_mode=False)

        if load_model:
            self.vae.load_json(os.path.join(vae_model_path_name, 'vae.json'))
            self.rnn.load_json(os.path.join(rnn_model_path_name, 'rnn.json'))

        self.action_space = spaces.Box(
            low=-1.0, high=1.0, shape=(2,), dtype=np.float32)

        self.outwidth = self.rnn.hps.seq_width
        self.obs_size = self.outwidth + model_rnn_size*model_state_space

        self.observation_space = Box(
            low=0, high=255, shape=(SCREEN_Y, SCREEN_X, 3))
        self.actual_observation_space = Box(
            low=-50., high=50., shape=(self.obs_size,))

        self.zero_state = self.rnn.sess.run(self.rnn.zero_state)
        self.seed()

        self.rnn_state = None
        self.z = None
        self.restart = None
        self.temperature = None
        self.frame_count = None
        self.viewer = None
        #  self.reset()

    def _sample_init_z(self):
        idx = self.np_random.randint(0, len(self.initial_mu_logvar))
        init_mu, init_logvar = self.initial_mu_logvar[idx]
        init_mu = np.array(init_mu)/10000.
        init_logvar = np.array(init_logvar)/10000.
        init_z = init_mu + np.exp(init_logvar/2.0) * \
            self.np_random.randn(*init_logvar.shape)
        return init_z

    def _current_state(self):
        if model_state_space == 2:
            return np.concatenate([self.z, self.rnn_state.c.flatten(), self.rnn_state.h.flatten()], axis=0)
        return np.concatenate([self.z, self.rnn_state.h.flatten()], axis=0)

    def _reset(self):
        self.temperature = TEMPERATURE
        self.rnn_state = self.zero_state
        self.z = self._sample_init_z()
        self.restart = 1
        self.frame_count = 0
        return self._current_state()

    def _seed(self, seed=None):
        if seed:
            tf.set_random_seed(seed)
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def _step(self, action):

        self.frame_count += 1

        prev_z = np.zeros((1, 1, self.outwidth))
        prev_z[0][0] = self.z

        prev_action = np.zeros((1, 1))
        prev_action[0] = action

        prev_restart = np.ones((1, 1))
        prev_restart[0] = self.restart

        s_model = self.rnn
        temperature = self.temperature

        feed = {s_model.input_z: prev_z,
                s_model.input_action: prev_action,
                s_model.input_restart: prev_restart,
                s_model.initial_state: self.rnn_state
                }

        [logmix, mean, logstd, logrestart, next_state] = \
            s_model.sess.run([s_model.out_logmix,
                              s_model.out_mean,
                              s_model.out_logstd,
                              s_model.out_restart_logits,
                              s_model.final_state],
                             feed)

        OUTWIDTH = self.outwidth

        # adjust temperatures
        logmix2 = np.copy(logmix)/temperature
        logmix2 -= logmix2.max()
        logmix2 = np.exp(logmix2)
        logmix2 /= logmix2.sum(axis=1).reshape(OUTWIDTH, 1)

        mixture_idx = np.zeros(OUTWIDTH)
        chosen_mean = np.zeros(OUTWIDTH)
        chosen_logstd = np.zeros(OUTWIDTH)
        for j in range(OUTWIDTH):
            idx = get_pi_idx(self.np_random.rand(), logmix2[j])
            mixture_idx[j] = idx
            chosen_mean[j] = mean[j][idx]
            chosen_logstd[j] = logstd[j][idx]

        rand_gaussian = self.np_random.randn(OUTWIDTH)*np.sqrt(temperature)
        next_z = chosen_mean+np.exp(chosen_logstd)*rand_gaussian

        next_restart = 0
        done = False
        if (logrestart[0] > 0):
            next_restart = 1
            done = True

        self.z = next_z
        self.restart = next_restart
        self.rnn_state = next_state

        reward = 1  # always return a reward of one if still alive.

        if self.frame_count >= self.max_frame:
            done = True

        return self._current_state(), reward, done, {}

    def _get_image(self, upsize=False):
        # decode the latent vector
        img = self.vae.decode(self.z.reshape(1, 64)) * 255.
        img = np.round(img).astype(np.uint8)
        img = img.reshape(64, 64, 3)
        if upsize:
            img = resize(img, (640, 640))
        return img

    def _render(self, mode='human', close=False):
        if not self.render_mode:
            return

        if close:
            if self.viewer is not None:
                self.viewer.close()
            self.viewer = None
            return

        if mode == 'rgb_array':
            img = self._get_image(upsize=True)
            return img

        elif mode == 'human':
            img = self._get_image(upsize=True)
            from gym.envs.classic_control import rendering
            if self.viewer is None:
                self.viewer = rendering.SimpleImageViewer()
                self.viewer.imshow(img)

if __name__ == "__main__":
    env = DuckieTownRNN()
    env._reset()
    env._render()

    @env.unwrapped.window.event
    def on_key_press(symbol, modifiers):
        if symbol == key.BACKSPACE or symbol == key.SLASH:
            print('RESET')
            env._reset()
            env._render()
        elif symbol == (key.PAGEUP or key.SEMICOLON):
            env.unwrapped.cam_angle[0] = 0
        elif symbol == key.ESCAPE:
            env.close()
            sys.exit(0)
    key_handler = key.KeyStateHandler()
    env.unwrapped.window.push_handlers(key_handler)

    def update(dt):
        action = np.array([0.0, 0.0])
        if key_handler[key.UP]:
            action = np.array([0.44, 0.0])
        if key_handler[key.DOWN]:
            action = np.array([-0.44, 0])
        if key_handler[key.LEFT]:
            action = np.array([0.35, +1])
        if key_handler[key.RIGHT]:
            action = np.array([0.35, -1])
        if key_handler[key.SPACE]:
            action = np.array([0, 0])
        # Speed boost
        if key_handler[key.LSHIFT]:
            action *= 1.5
        obs, reward, done, info = env._step(action)
        print('obs', obs.shape)
        print('step_count = %s, reward=%.3f' %
              (env.unwrapped.step_count, reward))

        if key_handler[key.RETURN]:
            from PIL import Image
            im = Image.fromarray(obs)
            im.save('screen.png')

        if done:
            print('done!')
            env._reset()
            env._render()

        env._render()

    pyglet.clock.schedule_interval(update, 1.0 / env.unwrapped.frame_rate)

    # Enter main event loop
    pyglet.app.run()
    env.close()