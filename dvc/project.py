import os
import csv
import stat
import json
import networkx as nx
from jsonpath_rw import parse

import dvc.cloud.base as cloud

from dvc.logger import Logger
from dvc.exceptions import DvcException
from dvc.stage import Stage
from dvc.config import Config
from dvc.state import LinkState
from dvc.lock import Lock
from dvc.scm import SCM, Base
from dvc.cache import Cache
from dvc.cloud.data_cloud import DataCloud
from dvc.system import System
from dvc.updater import Updater


class InitError(DvcException):
    def __init__(self, msg):
        super(InitError, self).__init__(msg)


class NotDvcFileError(DvcException):
    def __init__(self, path):
        msg = '\'{}\' is not a DVC file'.format(path)
        p = path + Stage.STAGE_FILE_SUFFIX
        if Stage.is_stage_file(p):
            msg += '. Maybe you meant \'{}\'?'.format(p)
        super(NotDvcFileError, self).__init__(msg)


class ReproductionError(DvcException):
    def __init__(self, dvc_file_name, ex):
        self.path = dvc_file_name
        msg = 'Failed to reproduce \'{}\''.format(dvc_file_name)
        super(ReproductionError, self).__init__(msg, cause=ex)


class Project(object):
    DVC_DIR = '.dvc'

    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(os.path.realpath(root_dir))
        self.dvc_dir = os.path.join(self.root_dir, self.DVC_DIR)

        self.config = Config(self.dvc_dir)
        self.scm = SCM(self.root_dir)
        self.lock = Lock(self.dvc_dir)
        self.link_state = LinkState(self.root_dir, self.dvc_dir)
        self.logger = Logger(self.config._config[Config.SECTION_CORE].get(Config.SECTION_CORE_LOGLEVEL, None))
        self.cache = Cache(self)
        self.cloud = DataCloud(cache=self.cache, config=self.config._config)
        self.updater = Updater(self.dvc_dir)

        self._ignore()

        self.updater.check()

    @staticmethod
    def init(root_dir=os.curdir, no_scm=False):
        """
        Initiate dvc project in directory.

        Args:
            root_dir: Path to project's root directory.

        Returns:
            Project instance.

        Raises:
            KeyError: Raises an exception.
        """
        root_dir = os.path.abspath(root_dir)
        dvc_dir = os.path.join(root_dir, Project.DVC_DIR)

        scm = SCM(root_dir)
        if type(scm) == Base and not no_scm:
            msg = "{} is not tracked by any supported scm tool(e.g. git).".format(root_dir)
            raise InitError(msg)

        os.mkdir(dvc_dir)

        config = Config.init(dvc_dir)
        proj = Project(root_dir)

        scm.add([config.config_file])
        if scm.ignore_file():
            scm.add([os.path.join(dvc_dir, scm.ignore_file())])

        return proj

    def _ignore(self):
        l = [self.link_state.state_file,
             self.link_state._lock_file.lock_file,
             self.lock.lock_file,
             self.config.config_local_file,
             self.updater.updater_file]

        if self.cache.local.cache_dir.startswith(self.root_dir):
            l += [self.cache.local.cache_dir]

        self.scm.ignore_list(l)

    def install(self):
        self.scm.install()

    def to_dvc_path(self, path):
        return os.path.relpath(path, self.root_dir)

    def add(self, fname):
        out = os.path.basename(os.path.normpath(fname))
        stage_fname = out + Stage.STAGE_FILE_SUFFIX
        cwd = os.path.dirname(os.path.abspath(fname))
        stage = Stage.loads(project=self,
                            cmd=None,
                            deps=[],
                            outs=[out],
                            fname=stage_fname,
                            cwd=cwd)

        stage.save()
        stage.dump()
        return stage

    def remove(self, target, outs_only=False):
        if not Stage.is_stage_file(target):
            raise NotDvcFileError(target)

        stage = Stage.load(self, target)
        if outs_only:
            stage.remove_outs()
        else:
            stage.remove()

        return stage

    def run(self,
            cmd=None,
            deps=[],
            outs=[],
            outs_no_cache=[],
            metrics_no_cache=[],
            fname=Stage.STAGE_FILE,
            cwd=os.curdir,
            no_exec=False):
        stage = Stage.loads(project=self,
                            fname=fname,
                            cmd=cmd,
                            cwd=cwd,
                            outs=outs,
                            outs_no_cache=outs_no_cache,
                            metrics_no_cache=metrics_no_cache,
                            deps=deps)
        if not no_exec:
            stage.run()
        stage.dump()
        return stage

    def imp(self, url, out):
        stage_fname = out + Stage.STAGE_FILE_SUFFIX
        cwd = os.path.dirname(os.path.abspath(out))
        stage = Stage.loads(project=self,
                            cmd=None,
                            deps=[url],
                            outs=[out],
                            fname=stage_fname,
                            cwd=cwd)

        stage.run()
        stage.dump()
        return stage


    def _reproduce_stage(self, stages, node, force):
        stage = stages[node].reproduce(force=force)
        if not stage:
            return []
        stage.dump()
        return [stage]

    def reproduce(self, target, recursive=True, force=False):
        stages = nx.get_node_attributes(self.graph(), 'stage')
        node = os.path.relpath(os.path.abspath(target), self.root_dir)
        if node not in stages:
            raise NotDvcFileError(target)

        if recursive:
            return self._reproduce_stages(stages, node, force)

        return self._reproduce_stage(stages, node, force)

    def _reproduce_stages(self, stages, node, force):
        result = []
        for n in nx.dfs_postorder_nodes(self.graph(), node):
            try:
                result += self._reproduce_stage(stages, n, force)
            except Exception as ex:
                raise ReproductionError(stages[n].relpath, ex)
        return result

    def checkout(self, target=None):
        if target:
            if not Stage.is_stage_file(target):
                raise NotDvcFileError(target)
            stages = [Stage.load(self, target)]
        else:
            self.link_state.remove_all()
            stages = self.stages()

        for stage in stages:
            stage.checkout()

    def _used_cache(self, target=None):
        cache_set = set()

        if target:
            stages = [Stage.load(self, target)]
        else:
            stages = self.stages()

        for stage in stages:
            for out in stage.outs:
                if out.path_info['scheme'] != 'local':
                    continue

                if not out.use_cache or not out.cache:
                    continue

                cache_set |= set([out.cache])
                if self.cache.local.is_dir_cache(out.cache) and os.path.isfile(out.cache):
                    dir_cache = self.cache.local.dir_cache(out.cache)
                    cache_set |= set(dir_cache.values())

        return list(cache_set)

    def gc(self):
        clist = self._used_cache()
        for cache in self.cache.local.all():
            if cache in clist:
                continue
            os.unlink(cache)
            self.logger.info(u'\'{}\' was removed'.format(self.to_dvc_path(cache)))

    def push(self, target=None, jobs=1, remote=None):
        return self.cloud.push(self._used_cache(target), jobs, remote=remote)

    def fetch(self, target=None, jobs=1, remote=None):
        return self.cloud.pull(self._used_cache(target), jobs, remote=remote)

    def pull(self, target=None, jobs=1, remote=None):
        ret = self.fetch(target, jobs, remote=remote)
        self.checkout()
        return ret

    def _local_status(self, target=None):
        status = {}

        if target:
            stages = [Stage.load(self, target)]
        else:
            stages = self.stages()

        for stage in stages:
            status.update(stage.status())

        return status

    def _cloud_status(self, target=None, jobs=1, remote=None):
        status = {}
        for target, ret in self.cloud.status(self._used_cache(target), jobs, remote=remote):
            if ret == cloud.STATUS_UNKNOWN or ret == cloud.STATUS_OK:
                continue

            prefix_map = {
                cloud.STATUS_DELETED: 'deleted',
                cloud.STATUS_MODIFIED: 'modified',
                cloud.STATUS_NEW: 'new',
            }

            path = os.path.relpath(target, self.cache.local.cache_dir)

            status[path] = prefix_map[ret]

        return status

    def status(self, target=None, jobs=1, cloud=False, remote=None):
        if cloud:
            return self._cloud_status(target, jobs, remote=remote)
        return self._local_status(target)

    def _read_metric_json(self, fd, json_path):
        parser = parse(json_path)
        return [x.value for x in parser.find(json.load(fd))]

    def _do_read_metric_tsv(self, reader, row, col):
        if col != None and row != None:
            return [reader[row][col]]
        elif col != None:
            return [r[col] for r in reader]
        elif row != None:
            return reader[row]
        return None

    def _read_metric_htsv(self, fd, htsv_path):
        col, row = htsv_path.split(',')
        row = int(row)
        reader = list(csv.DictReader(fd, delimiter='\t'))
        return self._do_read_metric_tsv(reader, row, col)

    def _read_metric_tsv(self, fd, tsv_path):
        col, row = tsv_path.split(',')
        row = int(row)
        col = int(col)
        reader = list(csv.reader(fd, delimiter='\t'))
        return self._do_read_metric_tsv(reader, row, col)

    def _read_metric(self, path, json_path=None, tsv_path=None, htsv_path=None):
        ret = None

        if not os.path.exists(path):
            return ret

        try: 
            with open(path, 'r') as fd:
                if json_path:
                    ret = self._read_metric_json(fd, json_path)
                elif tsv_path:
                    ret = self._read_metric_tsv(fd, tsv_path)
                elif htsv_path:
                    ret = self._read_metric_htsv(fd, htsv_path)
                else:
                    ret = fd.read()
        except Exception as exc:
            self.logger.debug('Unable to read metric in \'{}\''.format(path), exc)

        return ret

    def metrics_show(self, path=None, json_path=None, tsv_path=None, htsv_path=None, all_branches=False):
        res = {}
        saved = self.scm.active_branch()
        branches = self.scm.list_branches() if all_branches else [saved]
        for branch in branches:
            self.scm.checkout(branch)
            self.checkout()

            metrics = filter(lambda o: o.metric, self.outs())
            fnames = [path] if path else map(lambda o: o.path, metrics)
            for fname in fnames:
                rel = os.path.relpath(fname)
                metric = self._read_metric(fname,
                                           json_path=json_path,
                                           tsv_path=tsv_path,
                                           htsv_path=htsv_path)
                if not metric:
                    continue
                res[branch] = {}
                res[branch][rel] = metric

        self.scm.checkout(saved)
        self.checkout()

        for branch, val in res.items():
            self.logger.info('{}:'.format(branch))
            for fname, metric in val.items():
                self.logger.info('\t{}: {}'.format(fname, metric))

        return res

    def _metrics_modify(self, path, val):
        apath = os.path.abspath(path)
        for stage in self.stages():
            for out in stage.outs:
                if apath != out.path:
                    continue

                if out.path_info['scheme'] != 'local':
                    msg = 'Output \'{}\' scheme \'{}\' is not supported for metrics'
                    raise DvcException(msg.format(out.path, out.path_info['scheme']))

                if out.use_cache:
                    msg = 'Cached output \'{}\' is not supported for metrics'
                    raise DvcException(msg.format(out.rel_path))

                out.metric = val

            stage.dump()

    def metrics_add(self, path):
        self._metrics_modify(path, True)

    def metrics_remove(self, path):
        self._metrics_modify(path, False)

    def graph(self):
        G = nx.DiGraph()
        stages = self.stages()
        outs = self.outs()

        for stage in stages:
            node = os.path.relpath(stage.path, self.root_dir)
            G.add_node(node, stage=stage)
            for dep in stage.deps:
                for out in outs:
                    if out.path != dep.path and not dep.path.startswith(out.path + out.sep):
                        continue

                    dep_stage = out.stage
                    dep_node = os.path.relpath(dep_stage.path, self.root_dir)
                    G.add_node(dep_node, stage=dep_stage)
                    G.add_edge(node, dep_node)

        return G

    def stages(self):
        stages = []
        for root, dirs, files in os.walk(self.root_dir):
            for fname in files:
                path = os.path.join(root, fname)
                if not Stage.is_stage_file(path):
                    continue
                stages.append(Stage.load(self, path))
        return stages

    def outs(self):
        outs = []
        for stage in self.stages():
            outs += stage.outs
        return outs
