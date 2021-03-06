import datetime
import logging
import os
import socket
import subprocess

from hashlib import sha1

from WMCore.Services.Dashboard.DashboardAPI import DashboardAPI, DASHBOARDURL

from WMCore.Services.SiteDB.SiteDB import SiteDBJSON
from WMCore.Storage.SiteLocalConfig import loadSiteLocalConfig, SiteConfigError
from lobster import util

import time
import work_queue as wq

logger = logging.getLogger('lobster.cmssw.dashboard')

UNKNOWN = 'Unknown'
SUBMITTED = 'Pending'
DONE = 'Done'
RETRIEVED = 'Retrieved'
ABORTED = 'Aborted'
CANCELLED = 'Killed'
RUNNING = 'Running'
WAITING_RETRIEVAL = 'Waiting Retrieval'

# dictionary between work queue and dashboard status
status_map = {
    wq.WORK_QUEUE_TASK_UNKNOWN: UNKNOWN,
    wq.WORK_QUEUE_TASK_READY: SUBMITTED,
    wq.WORK_QUEUE_TASK_RUNNING: RUNNING,
    wq.WORK_QUEUE_TASK_WAITING_RETRIEVAL: WAITING_RETRIEVAL,
    wq.WORK_QUEUE_TASK_RETRIEVED: RETRIEVED,
    wq.WORK_QUEUE_TASK_DONE: DONE,
    wq.WORK_QUEUE_TASK_CANCELED: ABORTED
}


def patch_dash(dash):
    """Patch inconsistent WMCore

    """
    from WMCore.Services.Dashboard import apmon

    def new_apmon():
        apMonConf = {DASHBOARDURL: {'sys_monitoring': 0, 'general_info': 0, 'job_monitoring': 0}}
        try:
            return apmon.ApMon(apMonConf, 0)
        except Exception:
            logger.exception("can't create ApMon instance")
        return None
    dash.__dict__['_getApMonInstance'] = new_apmon


class Monitor(object):

    def setup(self, config):
        self._workflowid = util.checkpoint(config.workdir, 'id')

    def generate_ids(self, taskid):
        return "dummy", "dummy"

    def register_run(self):
        pass

    def register_tasks(self, ids):
        """Returns Dashboard MonitorJobID and SyncId."""
        for id_ in ids:
            yield None, None

    def update_task_status(self, data):
        pass

    def update_tasks(self, queue, exclude):
        pass

    def free(self):
        pass


class Dashboard(Monitor, util.Configurable):

    """
    Dashboard support for CMS.

    Will send task information to the CMS dashboard for global monitoring.

    Parameters
    ----------
    interval : int
        The interval in which status updates for all tasks should be sent.
    username : str or None
        The CMS username, or `None` (the default) to look it up
        automatically in the database.
    commonname : str or None
        The common/full name of the user, or `None` (the default) to obtain
        it from the proxy information.
    """

    _mutable = {}

    def __init__(self, interval=300, username=None, commonname=None):
        self.interval = interval
        self.__previous = 0
        self.__states = {}
        self.username = username if username else self.__get_user()
        self.commonname = commonname if commonname else self.__get_distinguished_name().rsplit('/CN=', 1)[1]

        self.__cmssw_version = 'Unknown'
        self.__executable = 'Unknown'
        self.__dash = None

        try:
            self._ce = loadSiteLocalConfig().siteName
        except SiteConfigError:
            logger.error("can't load siteconfig, defaulting to hostname")
            self._ce = socket.getfqdn()

    def __getstate__(self):
        state = dict(self.__dict__)
        del state['_Dashboard__dash']
        state['_Dashboard__dash'] = None
        return state

    def __get_distinguished_name(self):
        p = subprocess.Popen(["voms-proxy-info", "-identity"],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        id_, err = p.communicate()
        return id_.strip()

    def __get_user(self):
        db = SiteDBJSON({'cacheduration': 24, 'logger': logging.getLogger("WMCore")})
        return db.dnUserName(dn=self.__get_distinguished_name())

    def send(self, kind, data):
        if isinstance(data, dict):
            data = [data]
        if not self.__dash:
            lggr = logging.getLogger("WMCore")
            lggr.setLevel(logging.FATAL)
            with util.PartiallyMutable.unlock():
                self.__dash = DashboardAPI(logr=lggr)
                patch_dash(self.__dash)
        with self.__dash as dashboard:
            for params in data:
                params['MessageType'] = kind
                params['MessageTS'] = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
                dashboard.apMonSend(params)

    def setup(self, config):
        super(Dashboard, self).setup(config)
        if util.checkpoint(config.workdir, "sandbox cmssw version"):
            self.__cmssw_version = str(util.checkpoint(config.workdir, "sandbox cmssw version"))
        if util.checkpoint(config.workdir, "executable"):
            self.__executable = str(util.checkpoint(config.workdir, "executable"))

    def generate_ids(self, taskid):
        seid = 'https://{}/{}'.format(self._ce, sha1(self._workflowid).hexdigest()[-16:])
        monitorid = '{0}_{1}/{0}'.format(taskid, seid)
        syncid = 'https://{}//{}//12345.{}'.format(self._ce, self._workflowid, taskid)

        return monitorid, syncid

    def register_run(self):
        self.send('TaskMeta', {
            'taskId': self._workflowid,
            'jobId': 'TaskMeta',
            'tool': 'lobster',
            'tool_ui': os.environ.get('HOSTNAME', ''),
            'SubmissionType': 'direct',
            'JSToolVersion': '3.2.1',
            'scheduler': 'work_queue',
            'GridName': '/CN=' + self.commonname,
            'ApplicationVersion': self.__cmssw_version,
            'taskType': 'analysis',
            'vo': 'cms',
            'CMSUser': self.username,
            'user': self.username,
            'datasetFull': '',
            'resubmitter': 'user',
            'exe': self.__executable
        })
        self.free()

    def register_tasks(self, ids):
        data = []
        for id_ in ids:
            monitorid, syncid = self.generate_ids(id_)
            yield monitorid, syncid
            data.append({
                'taskId': self._workflowid,
                'jobId': monitorid,
                'sid': syncid,
                'GridJobSyncId': syncid,
                'broker': 'condor',
                'bossId': str(id),
                'SubmissionType': 'Direct',
                'TargetSE': 'Many_Sites',  # XXX This should be the SE where input data is stored
                'localId': '',
                'tool': 'lobster',
                'JSToolVersion': '3.2.1',
                'tool_ui': os.environ.get('HOSTNAME', ''),
                'scheduler': 'work_queue',
                'GridName': '/CN=' + self.commonname,
                'ApplicationVersion': self.__cmssw_version,
                'taskType': 'analysis',
                'vo': 'cms',
                'CMSUser': self.username,
                'user': self.username,
                # 'datasetFull': self.datasetPath,
                'resubmitter': 'user',
                'exe': self.__executable
            })
        self.send('JobMeta', data)

    def update_task_status(self, data):
        updates = []
        for id_, status in data:
            monitorid, syncid = self.generate_ids(id_)
            updates.append({
                'taskId': self._workflowid,
                'jobId': monitorid,
                'sid': syncid,
                'StatusValueReason': '',
                'StatusValue': status,
                'StatusEnterTime':
                "{0:%F_%T}".format(datetime.datetime.utcnow()),
                # Destination will be updated by the task once it sends a dashboard update.
                # in line with
                # https://github.com/dmwm/WMCore/blob/6f3570a741779d209f0f720647642d51b64845da/src/python/WMCore/Services/Dashboard/DashboardReporter.py#L136
                'StatusDestination': 'Unknown',
                'RBname': 'condor'
            })
        self.send('JobStatus', updates)

    def update_tasks(self, queue, exclude):
        """
        Update dashboard states for all tasks.
        This is done only if the task status changed.
        """
        report = time.time() > self.__previous + self.interval
        if not report:
            return
        with util.PartiallyMutable.unlock():
            self.__previous = time.time()

        try:
            ids = queue._task_table.keys()
        except Exception:
            raise

        data = []
        for id_ in ids:
            status = status_map[queue.task_state(id_)]
            if status in exclude:
                continue
            if not self.__states.get(id_) or self.__states.get(id_, status) != status:
                continue

            data.append((id_, status))
            self.__states.update({id_: status})
        self.update_task_status(data)
