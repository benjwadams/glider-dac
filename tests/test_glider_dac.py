import unittest
from glider_dac import app, db
from flask_mongokit import MongoKit
import os
from glider_util.bdb import UserDB

# TODO: replace with temp file location
tmp_usr_db = "tmp_users.db"

class FlaskTestGliderDAC(unittest.TestCase):

    def setUp(self):
        app.config["MONGODB_DATABASE"] = "gliderdac_test"
        app.config["TESTING"] = True
        app.config["DEBUG"] = True
        app.config["USER_DB_FILE"] = tmp_usr_db
        app.config['WTF_CSRF_ENABLED'] = False
        app.config["ADMINS"] = ["admin"]
        self.client = app.test_client()
        # test user DB file should not exist -- make this operation idempotent
        try:
            os.unlink(app.config['USER_DB_FILE'])
        except FileNotFoundError:
            pass

        UserDB.init_db(app.config['USER_DB_FILE'])
        self.user_db = UserDB(app.config['USER_DB_FILE'])
        # the BerkeleyDB library used here expects bytes to be supplied, not str
        self.user_db.set(b"admin", b"test")

        # could this be done without the app context -- just the config, for
        # example?
        with app.app_context():
            self.db = MongoKit(app)
            self.db.init_app(app)
            self.db.connect()
            self.db.users.insert({"username": "admin",
                                   "name": "admin",
                                   "email": "admin@noexist.notexisting",
                                   "organization": "admin"})
            self.db.institutions.insert({"name": "admin"})

    def tearDown(self):
        os.remove(app.config['USER_DB_FILE'])
        with app.app_context():
            # TODO: loop over collections automatically?
            self.db.users.drop()
            self.db.deployments.drop()
            self.db.institutions.drop()

    def test_provider_homepage(self):
        response = self.client.get("/", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

    def _login_admin(self):
        return self.client.post("/login", data={"username": "admin",
                                                    "password": "test"},
                                follow_redirects=True)

    def test_admin(self):
        # how to check redirect page location?  .location returns None
        response = self.client.get("/admin", follow_redirects=True)
        self.assertIn(b"Please log in to access this page", response.get_data())
        #Please log in to access this page.

        # test an invalid login
        response = self.client.post("/login", data={"username": "noexist",
                                                    "password": "noexist"},
                                    follow_redirects=True)
        # login failures return status 200! Doesn't make sense -- consider
        # changing in code
        self.assertEqual(response.status_code, 200)
        # Test with "Failed" string instead
        self.assertIn(b"Failed", response.get_data())
        # test admin login
        response = self.client.post("/login", data={"username": "admin",
                                                    "password": "test"},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        # check that we're logged in as an admin user
        response = self.client.get("/admin", follow_redirects=True)
        # TODO: determine some way to get Flask-Login current user context
        # after web requests

        #from flask_login import current_user
        #self.assertTrue(current_user.is_authenticated and
        #                current_user.is_admin)
