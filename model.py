import numpy as np
import tensorflow as tf
import numpy as np
import random
import copy

import envs

import config

'''
Coder: YuhangSong
Description: main hyper-paramters for ct
'''
MaxNumLstmPerConsi = 6
LstmSize = 256
MaxActionSpace = 30
MinActionSpace = 2
FinalDropOutKeepProb = 1.0
IfUpdateFlowControl = False
ScaleToCut = 0.01

def normalized_columns_initializer(std=1.0):
    def _initializer(shape, dtype=None, partition_info=None):
        out = np.random.randn(*shape).astype(np.float32)
        out *= std / np.sqrt(np.square(out).sum(axis=0, keepdims=True))
        return tf.constant(out)
    return _initializer

def flatten(x):
    return tf.reshape(x, [-1, np.prod(x.get_shape().as_list()[1:])])

def conv2d(x, num_filters, name, filter_size=(3, 3), stride=(1, 1), pad="SAME", dtype=tf.float32, collections=None):
    with tf.variable_scope(name):
        stride_shape = [1, stride[0], stride[1], 1]

        filter_shape = [filter_size[0], filter_size[1], int(x.get_shape()[3]), num_filters]

        # there are "num input feature maps * filter height * filter width"
        # inputs to each hidden unit
        fan_in = np.prod(filter_shape[:3])

        # each unit in the lower layer receives a gradient from:
        # "num output feature maps * filter height * filter width" /
        #   pooling size
        fan_out = np.prod(filter_shape[:2]) * num_filters

        # initialize weights with random weights
        w_bound = np.sqrt(6. / (fan_in + fan_out))

        w = tf.get_variable("W", filter_shape, dtype, tf.random_uniform_initializer(-w_bound, w_bound),
                            collections=collections)
        b = tf.get_variable("b", [1, 1, 1, num_filters], initializer=tf.zeros_initializer,
                            collections=collections)

        return tf.nn.conv2d(x, w, stride_shape, pad) + b

def linear(x, size, name, initializer=None, bias_init=0):
    w = tf.get_variable(name + "/w", [x.get_shape()[1], size], initializer=initializer)
    b = tf.get_variable(name + "/b", [size], initializer=tf.constant_initializer(bias_init))
    return tf.matmul(x, w) + b

def categorical_sample(logits, d, exploration=True):
    # value = tf.squeeze(tf.multinomial(logits - tf.reduce_max(logits, [1], keep_dims=True), 1), [1])
    temp = logits - tf.reduce_max(logits, [1], keep_dims=True)
    if exploration is True:
        temp = tf.multinomial(temp, 1)
    elif exploration is False:
        temp = tf.expand_dims(tf.argmax(temp, 1),-1)
    temp = tf.squeeze(temp, [1])
    temp = tf.one_hot(temp, d)
    return temp

def lstm_layer(x, size, step_size):

    '''lstm'''
    lstm = tf.contrib.rnn.BasicLSTMCell(size, state_is_tuple=True)

    '''state_init'''
    c_init = np.zeros((1, lstm.state_size.c), np.float32)
    h_init = np.zeros((1, lstm.state_size.h), np.float32)
    state_init = [c_init, h_init]

    '''state_in'''
    c_in = tf.placeholder(tf.float32, [None, lstm.state_size.c])
    h_in = tf.placeholder(tf.float32, [None, lstm.state_size.h])

    state_in_tuple = tf.contrib.rnn.LSTMStateTuple(c_in, h_in)

    '''run lstm'''
    x, lstm_state = tf.nn.dynamic_rnn(
        lstm, x, initial_state=state_in_tuple, sequence_length=step_size,
        time_major=False)

    '''state_out'''
    lstm_c, lstm_h = lstm_state
    state_out = [lstm_c[:1, :], lstm_h[:1, :]]

    return x, state_init, c_in, h_in, state_out

def conv_layers(x, num_layers, consi_layer_id):

    '''
    Coder: YuhangSong
    Description: To create multi layers of conv
    '''

    if(num_layers > 0):

        print("############### Create a sequence of conv layers ###############")

        for i in range(num_layers):

            print("####### Create conv layer: %d #######" % (i))

            # create a layer of conv
            x = tf.nn.elu(conv2d(x, 32, "l{}".format(i + 1), [3, 3], [2, 2]))

            # obtain lift_to_up:
            # the tensor to flow to upper consi layer from the lift part of this consi layer
            if(i==0):
                lift_to_up = x

    else:

        '''
        if num_layers is zero, output lift_to_up with x
        '''
        lift_to_up = x

    x = tf.expand_dims(flatten(x), 1)

    return x, lift_to_up

class LSTMPolicy(object):

    def __init__(self, ob_space, ac_space, env_id_str):

        with tf.variable_scope("GTN"):

            if config.project is 'g':
                '''convert env_id string to env_id num'''
                env_id_num = config.game_dic_all.index(env_id_str)

            '''placeholder for x and step_size'''
            self.x = tf.placeholder(tf.float32, [None] + list(ob_space))
            self.step_size = tf.placeholder(tf.int32, [None])

            '''nodes'''
            lift_in = range(config.consi_depth)
            lift_out = range(config.consi_depth)
            lift_to_up = range(config.consi_depth)

            right_in = range(config.consi_depth)
            right_out = range(config.consi_depth)
            right_to_down = range(config.consi_depth)

            self.state_init = []
            self.c_in = []
            self.h_in = []
            self.state_out = []

            lift_in[0] = self.x
            for consi_layer_id_pos in range(config.consi_depth):

                '''the construction of consi_right is from bottom to top'''
                consi_layer_id = consi_layer_id_pos

                '''creat a consi layer'''
                with tf.variable_scope("consi_layer_right_"+str(consi_layer_id)):
                    x = lift_in[consi_layer_id]
                    x, lift_to_up[consi_layer_id] = conv_layers(x              = x,
                                                                num_layers     = config.conv_depth,
                                                                consi_layer_id = 0)
                    x, state_init_t, c_in_t, h_in_t, state_out_t = lstm_layer(x         = x,
                                                                          size      = config.lstm_size[consi_layer_id],
                                                                          step_size = self.step_size)
                    lift_out[consi_layer_id] = x

                '''set lift_in for next consi layer'''
                if(consi_layer_id < (config.consi_depth - 1)):
                    lift_in[consi_layer_id + 1] = lift_to_up[consi_layer_id]

                self.state_init += [state_init_t]
                self.c_in += [c_in_t]
                self.h_in += [h_in_t]
                self.state_out += [state_out_t]

            '''bootstrap from consi_lift to consi_right'''
            right_in[config.consi_depth - 1] = lift_out[config.consi_depth - 1]

            for consi_layer_id_pos in range(config.consi_depth):

                '''the construction of consi_right is from top to bottom'''
                consi_layer_id = config.consi_depth - 1 - consi_layer_id_pos

                '''creat a consi layer'''
                with tf.variable_scope("consi_layer_right_"+str(consi_layer_id)):
                    right_out[consi_layer_id] = right_in[consi_layer_id]

                '''right_out flow to right_to_down'''
                right_to_down[consi_layer_id] = right_out[consi_layer_id]

                '''set lift_in for next consi layer'''
                if(consi_layer_id > 0):
                    right_in[consi_layer_id - 1] = tf.concat([right_to_down[consi_layer_id], lift_out[consi_layer_id - 1]],2)

            consi_output = tf.reshape(right_out[0], [-1, sum(config.lstm_size[:config.consi_depth])])

            if config.project is 'g':
                print('g project has seperate layer for each game')
                logits_all = range(len(config.game_dic_all))
                sample_all = range(len(config.game_dic_all))
                vf_all = range(len(config.game_dic_all))
                for env_id_i_num in range(len(config.game_dic_all)):
                    with tf.variable_scope("game_spec_layer_"+str(str(env_id_i_num))):
                        ac_space_i = config.game_dic_all_ac_space[config.game_dic_all[env_id_i_num]]
                        logits_all[env_id_i_num] = linear(consi_output, ac_space_i, "action", normalized_columns_initializer(0.01))
                        sample_all[env_id_i_num] = categorical_sample(logits_all[env_id_i_num], ac_space_i)[0, :]
                        vf_all[env_id_i_num] = tf.reshape(linear(consi_output, 1, "value", normalized_columns_initializer(1.0)), [-1])

                self.logits = logits_all[env_id_num]
                self.sample = sample_all[env_id_num]
                self.vf = vf_all[env_id_num]
            elif config.project is 'f':
                print('f project share the whole model')
                self.logits = linear(consi_output, ac_space, "action", normalized_columns_initializer(0.01))
                self.sample = categorical_sample(self.logits, ac_space, exploration=True)[0, :]
                self.sample_no_exploration = categorical_sample(self.logits, ac_space, exploration=False)[0, :]
                self.vf = tf.reshape(linear(consi_output, 1, "value", normalized_columns_initializer(1.0)), [-1])
                from config import if_learning_v
                self.if_learning_v = if_learning_v
                if self.if_learning_v is True:
                    self.v = tf.reshape(tf.nn.softplus(linear(consi_output, 1, "v", normalized_columns_initializer(1.0))), [-1])

            self.var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)

    def get_initial_features(self):
        return self.state_init

    def act(self, ob, state_in, exploration=True):
        sess = tf.get_default_session()
        feed_dict = {self.x: [ob], self.step_size: [1]}
        for consi_layer_id in range(config.consi_depth):
            feed_dict[self.c_in[consi_layer_id]] = state_in[consi_layer_id][0]
            feed_dict[self.h_in[consi_layer_id]] = state_in[consi_layer_id][1]
        if exploration is True:
            fetch_dic = [self.sample]
        elif exploration is False:
            fetch_dic = [self.sample_no_exploration]
        fetch_dic += [self.vf, self.state_out]
        if self.if_learning_v is True:
            fetch_dic += [self.v]
        return sess.run(fetch_dic, feed_dict)

    def value(self, ob, state_in):
        sess = tf.get_default_session()
        feed_dict = {self.x: [ob], self.step_size: [1]}
        for consi_layer_id in range(config.consi_depth):
            feed_dict[self.c_in[consi_layer_id]] = state_in[consi_layer_id][0]
            feed_dict[self.h_in[consi_layer_id]] = state_in[consi_layer_id][1]
        fetch_dic = [self.vf]
        if self.if_learning_v is True:
            fetch_dic += [self.v]
        return sess.run(fetch_dic, feed_dict)
