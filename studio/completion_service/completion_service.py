import sys
import os
import subprocess
import uuid
import logging
import time
import pickle
import tempfile

from studio import runner, model, fs_tracker

logging.basicConfig()


'''
class CompletionServiceManager:
    def __init__(
            self,
            config=None,
            resources_needed=None,
            cloud=None):
        self.config = config
        self.resources_needed = resources_needed
        self.wm = runner.get_worker_manager(config, cloud)
        self.logger = logging.getLogger(self.__class__.__name__)
        verbose = model.parse_verbosity(self.config['verbose'])
        self.logger.setLevel(verbose)

        self.queue = runner.get_queue(self.cloud, verbose)

        self.completion_services = {}

    def submitTask(self, experimentId, clientCodeFile, args):
        if experimentId not in self.completion_services.keys():
            self.completion_services[experimentId] = \
                CompletionService(
                    experimentId,
                    self.config,
                    self.resources_needed,
                    self.cloud).__enter__()

        return self.completion_services[experimentId].submitTask(
            clientCodeFile, args)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        for _, cs in self.completion_services.iter_items():
            cs.__exit__()
'''


class CompletionService:

    def __init__(
            self,
            experimentId,
            config=None,
            num_workers=1,
            resources_needed=None,
            cloud=None,
            cloud_timeout=100,
            bid='100%',
            ssh_keypair='peterz-k1',
            resumable=False,):

        self.config = model.get_config(config)
        self.cloud = None
        self.experimentId = experimentId
        self.project_name = "completion_service_" + experimentId

        self.queue_name = 'local'
        if cloud in ['gcloud', 'gcspot']:
            self.queue_name = 'pubsub_' + experimentId
        elif cloud in ['ec2', 'ec2spot']:
            self.queue_name = 'sqs_' + experimentId

        self.resources_needed = resources_needed
        self.wm = runner.get_worker_manager(self.config, cloud)
        self.logger = logging.getLogger(self.__class__.__name__)
        self.verbose_level = model.parse_verbosity(self.config['verbose'])
        self.logger.setLevel(self.verbose_level)

        self.queue = runner.get_queue(self.queue_name, self.cloud,
                                      self.verbose_level)

        self.cloud_timeout = cloud_timeout
        self.bid = bid
        self.ssh_keypair = ssh_keypair

        self.submitted = set([])
        self.num_workers = num_workers
        self.resumable = resumable

    def __enter__(self):
        if self.wm:
            self.logger.debug('Spinning up cloud workers')
            self.wm.start_spot_workers(
                self.queue_name,
                self.bid,
                self.resources_needed,
                start_workers=self.num_workers,
                queue_upscaling=True,
                ssh_keypair=self.ssh_keypair,
                timeout=self.cloud_timeout)
            self.p = None
        else:
            self.logger.debug('Starting local worker')
            self.p = subprocess.Popen([
                'studio-local-worker',
                '--verbose=%s' % self.config['verbose'],
                '--timeout=' + str(self.cloud_timeout)],
                close_fds=True)

        return self

    def __exit__(self, *args):
        if self.queue_name != 'local':
            self.queue.delete()

        if self.p:
            self.p.wait()

    def submitTaskWithFiles(self, clientCodeFile, args, files={}):
        old_cwd = os.getcwd()
        cwd = os.path.dirname(os.path.realpath(__file__))
        os.chdir(cwd)

        experiment_name = self.project_name + "_" + str(uuid.uuid4())

        tmpdir = tempfile.gettempdir()
        args_file = os.path.join(tmpdir, experiment_name + "_args.pkl")

        artifacts = {
            'retval': {
                'mutable': True
            },
            'clientscript': {
                'mutable': False,
                'local': clientCodeFile
            },
            'args': {
                'mutable': False,
                'local': args_file
            },
            'workspace': {
                'mutable': False,
                'local': fs_tracker.get_artifact_cache(
                    'workspace', experiment_name)
            }
        }

        for tag, name in files.iteritems():
            artifacts[tag] = {
                'mutable': False,
                'local': os.path.abspath(os.path.expanduser(name))
            }

        with open(args_file, 'w') as f:
            f.write(pickle.dumps(args))

        experiment = model.create_experiment(
            'completion_service_client.py',
            [self.config['verbose']],
            experiment_name=experiment_name,
            project=self.project_name,
            artifacts=artifacts,
            resources_needed=self.resources_needed)

        runner.submit_experiments(
            [experiment],
            config=self.config,
            logger=self.logger,
            cloud=self.cloud,
            queue_name=self.queue_name)

        self.submitted.add(experiment.key)
        os.chdir(old_cwd)

        return experiment_name

    def submitTask(self, clientCodeFile, args):
        return self.submitTaskWithFiles(clientCodeFile, args, {})

    def getResultsWithTimeout(self, timeout=0):

        total_sleep_time = 0
        sleep_time = 1

        while True:
            with model.get_db_provider(self.config) as db:
                if self.resumable:
                    experiments = db.get_project_experiments(self.project_name)
                else:
                    experiments = [db.get_experiment(key)
                                   for key in self.submitted]

            for e in experiments:
                if e.status == 'finished':
                    self.logger.debug('Experiment {} finished, getting results'
                                      .format(e.key))
                    with open(db.get_artifact(e.artifacts['retval'])) as f:
                        data = pickle.load(f)

                    if not self.resumable:
                        self.submitted.remove(e.key)
                    else:
                        with model.get_db_provider(self.config) as db:
                            db.delete_experiment(e.key)

                    return (e.key, data)

            if timeout == 0 or \
               (timeout > 0 and total_sleep_time > timeout):
                return None

            time.sleep(sleep_time)
            total_sleep_time += sleep_time

    def getResults(self, blocking=True):
        return self.getResultsWithTimeout(-1 if blocking else 0)
