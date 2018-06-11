#!/usr/bin/env python
import sys
sys.path.append('/usr/local/lib/python2.7/dist-packages')
import cv2
import go_vncdriver
import tensorflow as tf
import argparse
import logging
import os
from a3c import A3C
from envs import create_env
import config
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Disables write_meta_graph argument, which freezes entire process and is mostly useless.
class FastSaver(tf.train.Saver):
    def save(self, sess, save_path, global_step=None, latest_filename=None,
             meta_graph_suffix="meta", write_meta_graph=True):
        super(FastSaver, self).save(sess, save_path, global_step, latest_filename,
                                    meta_graph_suffix, False)

def run(args, server):

    from config import project, mode
    if (project is 'f') and (mode is 'on_line'):
        '''f project and on_line mode is special, log_dir is sperate by game (g) and subject (s)'''
        logdir = os.path.join(args.log_dir, 'train_g_'+str(args.env_id)+'_s_'+str(args.subject))
    else:
        '''normal log_dir'''
        logdir = os.path.join(args.log_dir, 'train')
    '''any way, log_dir is separate by work (task)'''
    summary_writer = tf.summary.FileWriter(logdir + "_%d" % args.task)
    '''log final log_dir'''
    logger.info("Events directory: %s_%s", logdir, args.task)

    '''create env'''
    env = create_env(args.env_id,
                     client_id=str(args.task),
                     remotes=args.remotes,
                     task=args.task,
                     subject=args.subject,
                     summary_writer=summary_writer)

    '''create trainer'''
    trainer = A3C(env, args.env_id, args.task)

    '''Variable names that start with "local" are not saved in checkpoints.'''
    variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local")]
    init_op = tf.variables_initializer(variables_to_save)
    init_all_op = tf.global_variables_initializer()
    saver = FastSaver(variables_to_save)

    variables_to_restore = [v for v in tf.all_variables() if v.name.startswith("global")]
    pre_train_saver = FastSaver(variables_to_restore)

    def init_fn(ses):
        logger.info("==========run init_fn============")
        ses.run(init_all_op)
        from config import project
        if project is 'f':
            from config import mode
            if mode is 'on_line':
                from config import if_restore_model
                if if_restore_model:
                    from config import model_to_restore
                    logger.info("'restore model from:'+restore_path")
                    restore_path = 'model_to_restore/'+model_to_restore
                    print('not support!!!')
                    # pre_train_saver.restore(ses,
                    #                         restore_path)

    config_tf = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(args.task)])

    '''whether call init var to avoid the bug'''
    if not ((project is 'f') and (mode is 'on_line')):
        '''project f and mode on_line not support GTN, so not have this problem'''
        if args.task is not config.task_chief:
            if config.project is 'g':
                print('this is g project, bug on ver init, since the graph is different across threads')
                tf.Session(server.target, config=config_tf).run(init_all_op)
            elif config.project is 'f':
                print('this is f project, no bug on ver init, since the praph is exactly same across threads')

    '''determine is_chief'''
    if (project is 'f') and (mode is 'on_line'):
        '''f project and on_line mode is special, it has one worker for each ps, so it is always the cheif'''
        is_chief = True
    else:
        '''normally, is_chief is determined by if the task is a cheif task (see config.py)'''
        is_chief = (args.task == config.task_chief)

    sv = tf.train.Supervisor(is_chief=is_chief,
                             logdir=logdir,
                             saver=saver,
                             summary_op=None,
                             init_op=init_op,
                             init_fn=init_fn,
                             summary_writer=summary_writer,
                             ready_op=tf.report_uninitialized_variables(variables_to_save),
                             global_step=trainer.global_step,
                             save_model_secs=30,
                             save_summaries_secs=30)


    logger.info(
        "Starting session. If this hangs, we're mostly likely waiting to connect to the parameter server. " +
        "One common cause is that the parameter server DNS name isn't resolving yet, or is misspecified.")

    '''start run'''
    with sv.managed_session(server.target, config=config_tf) as sess:

        '''start trainer'''
        trainer.start(sess, summary_writer)

        '''log global_step so that we can see if the model is restored successfully'''
        global_step = sess.run(trainer.global_step)
        logger.info("Starting training at step=%d", global_step)

        '''keep runing'''
        while not sv.should_stop() and True:
            trainer.process(sess)
            global_step = sess.run(trainer.global_step)

    '''Ask for all the services to stop.'''
    sv.stop()
    logger.info('reached %s steps. worker stopped.', global_step)

def cluster_spec(num_workers, num_ps, env_id=None, subject=None):
    """
    More tensorflow setup for data parallelism
    """
    from config import project, mode
    if (project is 'g') or ( (project is 'f') and ( (mode is 'off_line') or (mode is 'data_processor') ) ):
        cluster = {}
        port = 12222

        all_ps = []

        host = config.cluster_host[config.cluster_main]
        for _ in range(num_ps):
            all_ps.append('{}:{}'.format(host, port))
            port += 1
        cluster['ps'] = all_ps

        all_workers = []
        for host in config.cluster_host:
            for _ in range(num_workers):
                all_workers.append('{}:{}'.format(host, port))
                port += 1
        cluster['worker'] = all_workers

        return cluster
    elif (project is 'f') and (mode is 'on_line'):
        env_id_num = config.game_dic.index(env_id)
        position_offset = 12222
        position = (env_id_num * config.num_subjects + subject) * 2 + position_offset
        cluster = {}
        cluster['ps'] = ['127.0.0.1:'+str(position)]
        cluster['worker'] = ['127.0.0.1:'+str(position+1)]
        return cluster

def main(_):
    """
Setting up Tensorflow for data parallel work
"""

    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('-v', '--verbose', action='count', dest='verbosity', default=0, help='Set verbosity.')
    parser.add_argument('--task', default=0, type=int, help='Task index')
    parser.add_argument('--subject', default=None, type=int, help='subject index')
    parser.add_argument('--job-name', default="worker", help='worker or ps')
    parser.add_argument('--num-workers', default=1, type=int, help='Number of workers')
    parser.add_argument('--log-dir', default="/tmp/pong", help='Log directory path')
    parser.add_argument('--env-id', default="PongDeterministic-v3", help='Environment id')
    parser.add_argument('-r', '--remotes', default="1",
                        help='References to environments to create (e.g. -r 20), '
                             'or the address of pre-existing VNC servers and '
                             'rewarders to use (e.g. -r vnc://localhost:5900+15900,vnc://localhost:5901+15901)')

    args = parser.parse_args()
    from config import project, mode
    if (project is 'g') or ( (project is 'f') and ( (mode is 'off_line') or (mode is 'data_processor') ) ):
        spec = cluster_spec(args.num_workers, 1)
    elif (project is 'f') and (mode is 'on_line'):
        spec = cluster_spec(args.num_workers, 1, args.env_id, args.subject)
    cluster = tf.train.ClusterSpec(spec).as_cluster_def()


    if args.job_name == "worker":
        server = tf.train.Server(cluster, job_name="worker", task_index=args.task,
                                 config=tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=2))
        run(args, server)
    else:
        server = tf.train.Server(cluster, job_name="ps", task_index=args.task,
                                 config=tf.ConfigProto(device_filters=["/job:ps"]))

        server.join()

if __name__ == "__main__":
    tf.app.run()
