#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
glider_dac/models/deployment.py
Model definition for a Deployment
'''
from glider_dac import app, db, slugify, queue
from glider_dac.glider_emails import glider_deployment_check
from datetime import datetime
from flask_mongokit import Document
from bson.objectid import ObjectId
from rq import Queue, Connection, Worker
from shutil import rmtree
import os
import hashlib


@db.register
class Deployment(Document):
    __collection__ = 'deployments'
    use_dot_notation = True
    use_schemaless = True

    structure = {
        'name': str,
        'user_id': ObjectId,
        'username': str,  # The cached username to lightly DB load
        # The operator of this Glider. Shows up in TDS as the title.
        'operator': str,
        'deployment_dir': str,
        'estimated_deploy_date': datetime,
        'estimated_deploy_location': str,  # WKT text
        'wmo_id': str,
        'completed': bool,
        'created': datetime,
        'updated': datetime,
        'glider_name': str,
        'deployment_date': datetime,
        'archive_safe': bool,
        'checksum': str,
        'attribution': str,
        'delayed_mode': bool
    }

    default_values = {
        'created': datetime.utcnow,
        'completed': False,
        'archive_safe': True,
        'delayed_mode': False,
    }

    indexes = [
        {
            'fields': 'name',
            'unique': True,
        },
    ]

    def save(self):
        if self.username is None or self.username == '':
            user = db.User.find_one({'_id': self.user_id})
            self.username = user.username

        self.updated = datetime.utcnow()

        self.sync()
        Document.save(self)

    def delete(self):
        if os.path.exists(self.full_path):
            rmtree(self.full_path)
        if os.path.exists(self.public_erddap_path):
            rmtree(self.public_erddap_path)
        if os.path.exists(self.thredds_path):
            rmtree(self.thredds_path)
        Document.delete(self)

    @property
    def dap(self):
        '''
        Returns the THREDDS DAP URL to this deployment
        '''
        args = {
            'host': app.config['THREDDS'],
            'user': slugify(self.username),
            'deployment': slugify(self.name)
        }
        dap_url = "http://%(host)s/thredds/dodsC/deployments/%(user)s/%(deployment)s/%(deployment)s.nc3.nc" % args
        return dap_url

    @property
    def sos(self):
        '''
        Returns the URL to the NcSOS endpoint
        '''
        args = {
            'host': app.config['THREDDS'],
            'user': slugify(self.username),
            'deployment': slugify(self.name)
        }
        sos_url = "http://%(host)s/thredds/sos/deployments/%(user)s/%(deployment)s/%(deployment)s.nc3.nc?service=SOS&request=GetCapabilities&AcceptVersions=1.0.0" % args
        return sos_url

    @property
    def iso(self):
        name = slugify(self.name)
        iso_url = 'http://%(host)s/erddap/tabledap/%(name)s.iso19115' % {
            'host': app.config['PUBLIC_ERDDAP'], 'name': name}
        return iso_url

    @property
    def thredds(self):
        args = {
            'host': app.config['THREDDS'],
            'user': slugify(self.username),
            'deployment': slugify(self.name)
        }
        thredds_url = "http://%(host)s/thredds/catalog/deployments/%(user)s/%(deployment)s/catalog.html?dataset=deployments/%(user)s/%(deployment)s/%(deployment)s.nc3.nc" % args
        return thredds_url

    @property
    def erddap(self):
        args = {
            'host': app.config['PUBLIC_ERDDAP'],
            'user': slugify(self.username),
            'deployment': slugify(self.name)
        }
        erddap_url = "http://%(host)s/erddap/tabledap/%(deployment)s.html" % args
        return erddap_url

    @property
    def title(self):
        if self.operator is not None and self.operator != "":
            return self.operator
        else:
            return self.username

    @property
    def full_path(self):
        return os.path.join(app.config.get('DATA_ROOT'), self.deployment_dir)

    @property
    def public_erddap_path(self):
        return os.path.join(app.config.get('PUBLIC_DATA_ROOT'), self.deployment_dir)

    @property
    def thredds_path(self):
        return os.path.join(app.config.get('THREDDS_DATA_ROOT'), self.deployment_dir)

    def _hash_file(self, fname):
        """
        Calculates an md5sum of the passed in file (absolute path).

        Based on http://stackoverflow.com/a/4213255/84732
        """
        md5 = hashlib.md5()

        with open(fname, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 2048), b''):
                md5.update(chunk)

        return md5.hexdigest()

    def on_complete(self):
        """
        sync calls here to trigger any completion tasks.

        - write or remove complete.txt
        - generate/update md5 files (removed on not-complete)
        - link/unlink via bindfs to archive dir
        - schedule checker report against ERDDAP endpoint
        """
        # Save a file called "completed.txt"
        completed_file = os.path.join(self.full_path, "completed.txt")
        if self.completed is True:
            with open(completed_file, 'w') as f:
                f.write(" ")
        else:
            if os.path.exists(completed_file):
                os.remove(completed_file)

        # generate md5s of all data files on completion
        if self.completed:
            for dirpath, dirnames, filenames in os.walk(self.full_path):
                for f in filenames:
                    if (f in ["deployment.json", "wmoid.txt", "completed.txt"]
                        or f.endswith(".md5") or not f.endswith('.nc')):
                        continue

                    full_file = os.path.join(dirpath, f)
                    md5_file = full_file + ".md5"

                    # only calc md5 if it does not exist, this is expensive!
                    if os.path.exists(md5_file):
                        continue

                    md5_value = self._hash_file(full_file)

                    md5_file = full_file + ".md5"
                    with open(md5_file, 'w') as mf:
                        mf.write(md5_value)
            # schedule the checker job to kick off the compliance checker email
            # on the deployment when the deployment is completed
            # on_complete might be a misleading function name -- this section
            # can run any time there is a sync, so check if a checker run has already been executed
            if getattr(self, "compliance_check_passed", None) is not None:
                # eliminate force/always re-run?
                app.logger.info("Scheduling compliance check for completed "
                                "deployment {}".format(self.deployment_dir))
                queue.enqueue(glider_deployment_check,
                              kwargs={"deployment_dir": self.deployment_dir},
                              job_timeout=800)
        else:
            for dirpath, dirnames, filenames in os.walk(self.full_path):
                for f in filenames:
                    if f.endswith(".md5"):
                        os.unlink(os.path.join(dirpath, f))



    def calculate_checksum(self):
        '''
        Calculates a checksum for the deployment based on the MongoKit to_json()
        serialization and the modified time(s) of the dive file(s).
        '''
        md5 = hashlib.md5()
        # Now add the modified times for the dive files in the deployment directory
        # We dont MD5 every dive file here to save time
        for dirpath, dirnames, filenames in os.walk(self.full_path):
            for f in filenames:
                # could simply do `not f.endswith(.nc)`
                if (f in ["deployment.json", "wmoid.txt", "completed.txt"]
                    or f.endswith(".md5") or not f.endswith('.nc')):
                    continue
                mtime = os.path.getmtime(os.path.join(dirpath, f))
                mtime = datetime.utcfromtimestamp(mtime)

                md5.update(mtime.isoformat().encode('utf-8'))
        self.checksum = md5.hexdigest()

    def sync(self):
        if app.config.get('NODATA'):
            return
        if not os.path.exists(self.full_path):
            try:
                os.makedirs(self.full_path)
            except OSError:
                pass

        # Keep the WMO file updated if it is edited via the web form
        if self.wmo_id is not None and self.wmo_id != "":
            wmo_id_file = os.path.join(self.full_path, "wmoid.txt")
            with open(wmo_id_file, 'w') as f:
                f.write(self.wmo_id)

        # trigger any completed tasks if necessary
        self.on_complete()
        self.calculate_checksum()

        # Serialize Deployment model to disk
        json_file = os.path.join(self.full_path, "deployment.json")
        with open(json_file, 'w') as f:
            f.write(self.to_json())

    @classmethod
    def get_deployment_count_by_operator(cls):
        return [count for count in db.deployments.aggregate({'$group': {'_id': '$operator', 'count': {'$sum': 1}}}, cursor={})]
