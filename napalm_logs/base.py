# -*- coding: utf-8 -*-
'''
napalm-logs base
'''
from __future__ import absolute_import

# Import std lib
import os
import re
import imp
import sys
import time
import yaml
import logging
import threading
from multiprocessing import Process

# Import third party libs
# crypto
import nacl.utils
import nacl.secret
import nacl.signing
import nacl.encoding

# Import napalm-logs pkgs
import napalm_logs.utils
import napalm_logs.config as CONFIG
# processes
from napalm_logs.auth import NapalmLogsAuthProc
from napalm_logs.device import NapalmLogsDeviceProc
from napalm_logs.server import NapalmLogsServerProc
from napalm_logs.publisher import NapalmLogsPublisherProc
from napalm_logs.listener_proc import NapalmLogsListenerProc
# exceptions
from napalm_logs.exceptions import ConfigurationException

log = logging.getLogger(__name__)


class NapalmLogs:
    def __init__(self,
                 address='0.0.0.0',
                 port=514,
                 listener='udp',
                 transport='zmq',
                 publish_address='0.0.0.0',
                 publish_port=49017,
                 auth_address='0.0.0.0',
                 auth_port=49018,
                 certificate=None,
                 keyfile=None,
                 disable_security=False,
                 config_path=None,
                 config_dict=None,
                 extension_config_path=None,
                 extension_config_dict=None,
                 log_level='warning',
                 log_format='%(asctime)s,%(msecs)03.0f [%(name)-17s][%(levelname)-8s] %(message)s',
                 listener_opts={},
                 logger=None,
                 logger_opts={},
                 publisher_opts={},
                 device_blacklist=[],
                 device_whitelist=[],
                 hwm=None,
                 device_worker_processes=1):
        '''
        Init the napalm-logs engine.

        :param address: The address to bind the syslog client. Default: 0.0.0.0.
        :param port: Listen port. Default: 514.
        :param listener: Listen type. Default: udp.
        :param publish_address: The address to bing when publishing the OC
                                 objects. Default: 0.0.0.0.
        :param publish_port: Publish port. Default: 49017.
        '''
        self.address = address
        self.port = port
        self.listener_type = listener
        self.publish_address = publish_address
        self.publish_port = publish_port
        self.auth_address = auth_address
        self.auth_port = auth_port
        self.certificate = certificate
        self.keyfile = keyfile
        self.disable_security = disable_security
        self.config_path = config_path
        self.config_dict = config_dict
        self.transport = transport
        self.extension_config_path = extension_config_path
        self.extension_config_dict = extension_config_dict
        self.log_level = log_level
        self.log_format = log_format
        self.listener_opts = listener_opts
        self.logger = logger
        self.logger_opts = logger_opts
        self.publisher_opts = publisher_opts
        self.device_whitelist = device_whitelist
        self.device_blacklist = device_blacklist
        self.device_worker_processes = device_worker_processes
        self.opts = {}
        self.opts['hwm'] = CONFIG.ZMQ_INTERNAL_HWM if hwm is None else hwm
        # Setup the environment
        self._setup_log()
        self._build_config()
        self._verify_config()
        # Private vars
        self.__priv_key = None
        self.__signing_key = None
        self._processes = []
        self.up = True

    def _exit_gracefully(self, signum, _):
        self.stop_engine()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.stop_engine()
        if exc_type is not None:
            log.error('Exiting due to unhandled exception', exc_info=True)
            self.__raise_clean_exception(exc_type, exc_value, exc_traceback)

    def _setup_log(self):
        '''
        Setup the log object.
        '''
        logging_level = CONFIG.LOGGING_LEVEL.get(self.log_level.lower())
        logging.basicConfig(format=self.log_format,
                            level=logging_level)

    def _whitelist_blacklist(self, os_name):
        '''
        Determines if the OS should be ignored,
        depending on the whitelist-blacklist logic
        configured by the user.
        '''
        return (self.device_whitelist and
                hasattr(self.device_whitelist, '__iter__') and
                os_name not in self.device_whitelist) or\
               (self.device_blacklist and
                hasattr(self.device_blacklist, '__iter__') and
                os_name in self.device_blacklist)

    @staticmethod
    def _extract_yaml_docstring(stream):
        '''
        Extract the comments at the top of the YAML file,
        from the stream handler.
        Return the extracted comment as string.
        '''
        comment_lines = []
        lines = stream.read().splitlines()
        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                continue
            if line_strip.startswith('#'):
                comment_lines.append(
                    line_strip.replace('#', '', 1).strip()
                )
            else:
                break
        return ' '.join(comment_lines)

    def _load_config(self, path):
        '''
        Read the configuration under a specific path
        and return the object.
        '''
        config = {}
        log.debug('Reading configuration from %s', path)
        if not os.path.isdir(path):
            msg = (
                'Unable to read from {path}: '
                'the directory does not exist!'
            ).format(path=path)
            log.error(msg)
            raise IOError(msg)
        # The directory tree should look like the following:
        # .
        # ├── __init__.py
        # ├── eos
        # │   └── init.yml
        # ├── iosxr
        # │   └── __init__.py
        # ├── junos
        # │   └── init.yml
        # │   └── bgp_read_message.py
        # │   └── BGP_PREFIX_THRESH_EXCEEDED.py
        # └── nxos
        #     └── init.yml
        os_subdirs = [sdpath[0] for sdpath in os.walk(path)][1:]
        if not os_subdirs:
            log.error('%s does not contain any OS subdirectories', path)
        for os_dir in os_subdirs:
            os_name = os.path.split(os_dir)[1]  # the network OS name
            if os_name.startswith('__'):
                log.debug('Ignoring %s', os_name)
                continue
            if self._whitelist_blacklist(os_name):
                log.debug('Not building config for %s (whitelist-blacklist logic)', os_name)
                # Ignore devices that are not in the whitelist (if defined),
                #   or those operating systems that are on the blacklist.
                # This way we can prevent starting unwanted sub-processes.
                continue
            log.debug('Building config for %s:', os_name)
            log.debug('='*40)
            if os_name not in config:
                config[os_name] = {}
            files = os.listdir(os_dir)
            # Read all files under the OS dir
            for file_ in files:
                log.debug('Inspecting %s', file_)
                file_name, file_extension = os.path.splitext(file_)
                file_extension = file_extension.replace('.', '')
                filepath = os.path.join(os_dir, file_)
                comment = ''
                if file_extension in ('yml', 'yaml'):
                    try:
                        log.debug('Loading %s as YAML', file_)
                        with open(filepath, 'r') as fstream:
                            cfg = yaml.load(fstream)
                            # Reposition at the top and read the comments.
                            if file_name not in CONFIG.OS_INIT_FILENAMES:
                                # If the file name is not a profile init.
                                fstream.seek(0)
                                comment = self._extract_yaml_docstring(fstream)
                                if 'messages' in cfg:
                                    for message in cfg['messages']:
                                        message['__doc__'] = comment
                            napalm_logs.utils.dictupdate(config[os_name], cfg)
                    except yaml.YAMLError as yamlexc:
                        log.error('Invalid YAML file: %s', filepath, exc_info=True)
                        if file_name in CONFIG.OS_INIT_FILENAMES:
                            # Raise exception and break only when the init file is borked
                            #   otherwise, it will try loading best efforts.
                            raise IOError(yamlexc)
                elif file_extension == 'py':
                    log.debug('Lazy loading Python module %s', file_)
                    mod_fp, mod_file, mod_data = imp.find_module(file_name, [os_dir])
                    mod = imp.load_module(file_name, mod_fp, mod_file, mod_data)
                    if file_name in CONFIG.OS_INIT_FILENAMES:
                        # Init file defined as Python module
                        log.debug('%s seems to be a Python profiler', filepath)
                        # Init files require to define the `extract` function.
                        # Sample init file:
                        # def extract(message):
                        #     return {'tag': 'A_TAG', 'host': 'hostname'}
                        if hasattr(mod, CONFIG.INIT_RUN_FUN) and\
                           hasattr(getattr(mod, CONFIG.INIT_RUN_FUN), '__call__'):
                            # if extract is defined and is callable
                            if 'prefixes' not in config[os_name]:
                                config[os_name]['prefixes'] = []
                            config[os_name]['prefixes'].append({
                                'values': {'tag': ''},
                                'line': '',
                                '__python_fun__': getattr(mod, CONFIG.INIT_RUN_FUN),
                                '__python_mod__': filepath  # Will be used for debugging
                            })
                            log.info('Adding the prefix function defined under %s to %s',
                                     filepath, os_name)
                        elif file_name != '__init__':
                            # If __init__.py does not have the extractor function, no problem.
                            log.warning('%s does not have the "%s" function defined. Ignoring.',
                                        filepath, CONFIG.INIT_RUN_FUN)
                    else:
                        # Other python files require the `emit` function.
                        if hasattr(mod, '__tag__'):
                            mod_tag = getattr(mod, '__tag__')
                        else:
                            log.info('%s does not have __tag__, defaulting the tag to %s', filepath, file_name)
                            mod_tag = file_name
                        if hasattr(mod, '__error__'):
                            mod_err = getattr(mod, '__error__')
                        else:
                            log.info('%s does not have __error__, defaulting the error to %s', filepath, file_name)
                            mod_err = file_name
                        if hasattr(mod, '__match_on__'):
                            err_match = getattr(mod, '__match_on__')
                        else:
                            err_match = 'tag'
                        model = CONFIG.OPEN_CONFIG_NO_MODEL
                        if hasattr(mod, '__yang_model__'):
                            model = getattr(mod, '__yang_model__')
                        log.debug('Mathing on %s', err_match)
                        if hasattr(mod, CONFIG.CONFIG_RUN_FUN) and\
                           hasattr(getattr(mod, CONFIG.CONFIG_RUN_FUN), '__call__'):
                            log.debug('Adding %s with tag:%s, error:%s, matching on:%s',
                                      file_, mod_tag, mod_err, err_match)
                            # the structure below must correspond to the VALID_CONFIG structure enforcement
                            if 'messages' not in config[os_name]:
                                config[os_name]['messages'] = []
                            config[os_name]['messages'].append({
                                'tag': mod_tag,
                                'error': mod_err,
                                'match_on': err_match,
                                '__doc__': mod.__doc__,
                                '__python_fun__': getattr(mod, CONFIG.CONFIG_RUN_FUN),
                                '__python_mod__': filepath,  # Will be used for debugging
                                'line': '',
                                'model': model,
                                'values': {},
                                'mapping': {'variables': {}, 'static': {}}
                            })
                        else:
                            log.warning('%s does not have the "%s" function defined. Ignoring.',
                                        filepath, CONFIG.CONFIG_RUN_FUN)
                else:
                    log.info('Ignoring %s (extension not allowed)', filepath)
            log.debug('-'*40)
        if not config:
            msg = 'Could not find proper configuration files under {path}'.format(path=path)
            log.error(msg)
            raise IOError(msg)
        log.debug('Complete config:')
        log.debug(config)
        log.debug('ConfigParserg size in bytes: %d', sys.getsizeof(config))
        return config

    @staticmethod
    def _raise_config_exception(error_string):
        log.error(error_string, exc_info=True)
        raise ConfigurationException(error_string)

    def _compare_values(self, value, config, dev_os, key_path):
        if 'line' not in value or\
           'values' not in value or\
           '__python_fun__' not in value:  # Check looks good when using a Python-defined profile.
            return
        from_line = re.findall('\{(\w+)\}', config['line'])
        if set(from_line) == set(config['values']):
            return
        if config.get('error'):
            error = 'The "values" do not match variables in "line" for {}:{} in {}'.format(
                ':'.join(key_path),
                config.get('error'),
                dev_os
            )
        else:
            error = 'The "values" do not match variables in "line" for {} in {}'.format(
                ':'.join(key_path),
                dev_os
            )
        self._raise_config_exception(error)

    def _verify_config_key(self, key, value, valid, config, dev_os, key_path):
        key_path.append(key)
        if config.get(key, False) is False:
            self._raise_config_exception(
                'Unable to find key "{}" for {}'.format(':'.join(key_path), dev_os))
        if isinstance(value, type):
            if not isinstance(config[key], value):
                self._raise_config_exception(
                    'Key "{}" for {} should be {}'.format(':'.join(key_path), dev_os, value))
        elif isinstance(value, dict):
            if not isinstance(config[key], dict):
                self._raise_config_exception(
                    'Key "{}" for {} should be of type <dict>'.format(':'.join(key_path), dev_os))
            self._verify_config_dict(value, config[key], dev_os, key_path)
            # As we have already checked that the config below this point is correct, we know that "line" and "values"
            # exists in the config if they are present in the valid config
            self._compare_values(value, config[key], dev_os, key_path)
        elif isinstance(value, list):
            if not isinstance(config[key], list):
                self._raise_config_exception(
                    'Key "{}" for {} should be of type <list>'.format(':'.join(key_path), dev_os))
            for item in config[key]:
                self._verify_config_dict(value[0], item, dev_os, key_path)
                self._compare_values(value[0], item, dev_os, key_path)
        key_path.remove(key)

    def _verify_config_dict(self, valid, config, dev_os, key_path=None):
        '''
        Verify if the config dict is valid.
        '''
        if not key_path:
            key_path = []
        for key, value in valid.items():
            self._verify_config_key(key, value, valid, config, dev_os, key_path)

    def _verify_config(self):
        '''
        Verify that the config is correct
        '''
        if not self.config_dict:
            self._raise_config_exception('No config found')
        # Check for device conifg, if there isn't anything then just log, do not raise an exception
        for dev_os, dev_config in self.config_dict.items():
            if not dev_config:
                log.warning('No config found for %s', dev_os)
                continue
            # Compare the valid opts with the conifg
            self._verify_config_dict(CONFIG.VALID_CONFIG, dev_config, dev_os)
        log.debug('Read the config without error \o/')

    def _build_config(self):
        '''
        Build the config of the napalm syslog parser.
        '''
        if not self.config_dict:
            if not self.config_path:
                # No custom config path requested
                # Read the native config files
                self.config_path = os.path.join(
                    os.path.dirname(os.path.realpath(__file__)),
                    'config'
                )
            log.info('Reading the configuration from %s', self.config_path)
            self.config_dict = self._load_config(self.config_path)
        if not self.extension_config_dict and\
           self.extension_config_path and\
           os.path.normpath(self.extension_config_path) != os.path.normpath(self.config_path):  # same path?
            # When extension config is not sent as dict
            # But `extension_config_path` is specified
            log.info('Reading extension configuration from %s', self.extension_config_path)
            self.extension_config_dict = self._load_config(self.extension_config_path)
        if self.extension_config_dict:
            napalm_logs.utils.dictupdate(self.config_dict, self.extension_config_dict)  # deep merge

    def _respawn_when_dead(self, start_fun, *args, **kwargs):
        '''
        Start a process and restart when dead.
        '''
        proc = start_fun(*args, **kwargs)
        pid = proc.pid
        log.debug('Starting keepalive for %s (%s)', proc._name, pid)
        while True:
            time.sleep(5)
            proc_file = os.path.join('/proc', str(pid), 'stat')
            try:
                proc_flag = open(proc_file).readline().split()[2]
            except IOError:
                log.warning('The following error may not be critical:')
                log.warning('Unable to read %s', proc_file, exc_info=True)
                proc_flag = 'X'
            if proc_flag in CONFIG.PROC_DEAD_FLAGS:
                log.warning('Process %s with %d is dead, restarting', proc._name, pid)
                log.debug('Killing the previous process')
                try:
                    os.kill(pid, 9)
                except OSError as err:
                    log.error('Unable to kill %d', pid)
                    if err.strerror == 'No such process':
                        log.warning('The following error may not be critical:')
                        log.warning('Unable to kill PID %s', pid, exc_info=True)
                # Restarting proc
                proc = start_fun(*args, **kwargs)
                log.warning('%s (PID %d) restarted with PID %d', proc._name, pid, proc.pid)
                pid = proc.pid

    def _start_auth_proc(self):
        '''
        Start the authenticator process.
        '''
        log.debug('Computing the signing key hex')
        verify_key = self.__signing_key.verify_key
        sgn_verify_hex = verify_key.encode(encoder=nacl.encoding.HexEncoder)
        log.debug('Starting the authenticator subprocess')
        auth = NapalmLogsAuthProc(self.certificate,
                                  self.keyfile,
                                  self.__priv_key,
                                  sgn_verify_hex,
                                  self.auth_address,
                                  self.auth_port)
        proc = Process(target=auth.start)
        proc.start()
        proc.description = 'Auth process'
        log.debug('Started auth process as %s with PID %s', proc._name, proc.pid)
        return proc

    def _start_lst_proc(self):
                        # pipe):
        '''
        Start the listener process.
        '''
        log.debug('Starting the listener process')
        # Get the correct listener class
        listener = NapalmLogsListenerProc(self.opts,
                                          self.address,
                                          self.port,
                                          self.listener_type,
                                          # pipe,
                                          listener_opts=self.listener_opts)
        proc = Process(target=listener.start)
        proc.start()
        proc.description = 'Listener process'
        log.debug('Started listener process as %s with PID %s', proc._name, proc.pid)
        return proc

    def _start_srv_proc(self,
                        started_os_proc):
                        # pipe,
                        # os_pipes):
        '''
        Start the server process.
        '''
        log.debug('Starting the server process')
        server = NapalmLogsServerProc(self.opts,
                                      self.config_dict,
                                      started_os_proc,
                                      # pipe,
                                      # os_pipes,
                                      self.logger,
                                      self.logger_opts,
                                      self.publisher_opts)
        proc = Process(target=server.start)
        proc.start()
        proc.description = 'Server process'
        log.debug('Started server process as %s with PID %s', proc._name, proc.pid)
        return proc

    def _start_pub_proc(self):
                        # pub_pipe):
        '''
        Start the publisher process.
        '''
        log.info('Starting the publisher process')
        publisher = NapalmLogsPublisherProc(self.opts,
                                            self.publish_address,
                                            self.publish_port,
                                            self.transport,
                                            # pub_pipe,
                                            self.__priv_key,
                                            self.__signing_key,
                                            self.publisher_opts,
                                            disable_security=self.disable_security)
        proc = Process(target=publisher.start)
        proc.start()
        proc.description = 'Publisher process'
        log.debug('Started publisher process as %s with PID %s', proc._name, proc.pid)
        return proc

    def _start_dev_proc(self,
                        device_os,
                        device_config):
                        # device_pipe,
                        # dev_pub_pipe):
        '''
        Start the device worker process.
        '''
        log.info('Starting the child process for %s', device_os)
        dos = NapalmLogsDeviceProc(device_os,
                                   self.opts,
                                   device_config,
                                   # device_pipe,
                                   # dev_pub_pipe,
                                   self.publisher_opts)
        os_proc = Process(target=dos.start)
        os_proc.start()
        os_proc.description = '%s device process' % device_os
        log.debug('Started process %s for %s, having PID %s', os_proc._name, device_os, os_proc.pid)
        return os_proc

    def start_engine(self):
        '''
        Start the child processes (one per device OS)
        '''
        if self.disable_security is True:
            log.warning('***Not starting the authenticator process due to disable_security being set to True***')
        else:
            log.debug('Generating the private key')
            self.__priv_key = nacl.utils.random(nacl.secret.SecretBox.KEY_SIZE)
            log.debug('Generating the signing key')
            self.__signing_key = nacl.signing.SigningKey.generate()
            # start the keepalive thread for the auth sub-process
            self._processes.append(self._start_auth_proc())
        # publisher process start
        # pub_pipe, dev_pub_pipe = Pipe(duplex=False)
        self._processes.append(self._start_pub_proc())
        # device process start
        log.info('Starting child processes for each device type')
        # os_pipes = {}
        if self.publisher_opts.get('send_unknown'):
            # Explicitly requested to send messages from unidentified devices.
            log.info('Starting an additional process to publish messages from unknown operating systems.')
            self.config_dict[CONFIG.UNKNOWN_DEVICE_NAME] = {}
        started_os_proc = []
        for device_os, device_config in self.config_dict.items():
            if self._whitelist_blacklist(device_os):
                log.debug('Not starting process for %s (whitelist-blacklist logic)', device_os)
                # Ignore devices that are not in the whitelist (if defined),
                #   or those operating systems that are on the blacklist.
                # This way we can prevent starting unwanted sub-processes.
                continue
            # device_pipe, srv_pipe = Pipe(duplex=False)
            log.debug('Will start %d worker process(es) for %s', self.device_worker_processes, device_os)
            for proc_index in range(self.device_worker_processes):
                self._processes.append(self._start_dev_proc(device_os,
                                                            device_config))
                                                            # device_pipe,    # noqa
                                                            # dev_pub_pipe))  # noqa
            started_os_proc.append(device_os)
            # os_pipes[device_os] = srv_pipe
        # start server process
        # srv_pipe, lst_pipe = Pipe(duplex=False)
        self._processes.append(self._start_srv_proc(started_os_proc))
        # start listener process
        self._processes.append(self._start_lst_proc())
        thread = threading.Thread(target=self._check_children)
        thread.start()

    def _check_children(self):
        '''
        Check all of the child processes are still running
        '''
        while self.up:
            time.sleep(1)
            for process in self._processes:
                if process.is_alive() is True:
                    continue
                log.debug('%s is dead. Stopping the napalm-logs engine.', process.description)
                self.stop_engine()

    def stop_engine(self):
        self.up = False
        log.info('Shutting down the engine')
        # Set SIGTERM to all child processes, then join them
        for proc in self._processes:
            proc.terminate()
        for proc in self._processes:
            proc.join()
