from flask import Flask, render_template, session, request, Response, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from functools import wraps
from datetime import timedelta
import time
import requests
import settings
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
app.secret_key = settings.secret_key
app.config.from_object(settings.configClass)
db = SQLAlchemy(app)


# ============================================
# Logging
# ============================================

formatter = logging.Formatter(settings.LOG_FORMAT)
handler = RotatingFileHandler(
    settings.LOG_FILE,
    maxBytes=settings.LOG_MAX_BYTES,
    backupCount=settings.LOG_BACKUP_COUNT
)
handler.setLevel(logging.getLevelName(settings.LOG_LEVEL))
handler.setFormatter(formatter)
app.logger.addHandler(handler)

# ============================================
# DB Model
# ============================================


class Users(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, unique=True)
    refresh_key = db.Column(db.String(255))
    expires_in = db.Column(db.BigInteger)

    def __init__(self, user_id, refresh_key, expires_in):
        self.user_id = user_id
        self.refresh_key = refresh_key
        self.expires_in = expires_in

    def __repr__(self):
        return '<User %r>' % self.user_id


# ============================================
# Utility Functions
# ============================================

def return_error(msg):
    return render_template('error.htm.j2', msg=msg)


def check_valid_user(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        """
        Decorator to check if the user is allowed access to the app.

        If user is allowed, return the decorated function.
        Otherwise, return an error page with corresponding message.
        """
        if request.form:
            session.permanent = True
            # 1 hour long session
            app.permanent_session_lifetime = timedelta(minutes=60)
            session['course_id'] = request.form.get('custom_canvas_course_id')
            session['canvas_user_id'] = request.form.get('custom_canvas_user_id')
            roles = request.form['roles']

            if "Administrator" in roles:
                session['admin'] = True
                session['instructor'] = True
            elif 'admin' in session:
                # remove old admin key in the session
                session.pop('admin', None)

            if "Instructor" in roles:
                session['instructor'] = True
            elif 'instructor' in session:
                # remove old instructor key from the session
                session.pop('instructor', None)

        # no session and no request
        if not session:
            if not request.form:
                app.logger.warning("No session and no request. Not allowed.")
                return return_error('No session or request provided.')

        # no canvas_user_id
        if not request.form.get('custom_canvas_user_id') and 'canvas_user_id' not in session:
            app.logger.warning("No canvas user ID. Not allowed.")
            return return_error('No canvas uer ID provided.')

        # no course_id
        if not request.form.get('custom_canvas_course_id') and 'course_id' not in session:
            app.logger.warning("No course ID. Not allowed.")
            return return_error('No course_id provided.')

        # If they are neither instructor or admin, they're not in the right place

        if 'instructor' and 'admin' not in session:
            app.logger.warning("Not enrolled as Teacher or an Admin. Not allowed.")
            return return_error('''You are not enrolled in this course as a Teacher or Designer.
            Please refresh and try again. If this error persists, please contact
            Webcourses Support.''')

        return f(*args, **kwargs)
    return decorated_function


# ============================================
# Web Views / Routes
# ============================================


@app.route('/index', methods=['POST', 'GET'])
def index():
    # Cool, we got through
    msg = "hi!"
    session['course_id'] = request.form.get('custom_canvas_course_id')
    session['user_id'] = request.form.get('custom_canvas_user_id')

    return render_template('index.htm.j2', msg=msg)


# OAuth login
# Redirect URI


@app.route('/oauthlogin', methods=['POST', 'GET'])
def oauth_login():
    code = request.args.get('code')
    payload = {
        'grant_type': 'authorization_code',
        'client_id': settings.oauth2_id,
        'redirect_uri': settings.oauth2_uri,
        'client_secret': settings.oauth2_key,
        'code': code
    }
    r = requests.post(settings.BASE_URL+'login/oauth2/token', data=payload)

    if r.status_code == 500:
        # Canceled oauth or server error
        app.logger.error(
            '''Status code 500 from oauth, authentication error\n
            User ID: None Course: None \n {0} \n Request headers: {1} \n Session: {2}'''.format(
                r.url, r.headers, session
            )
        )

        msg = '''Authentication error,
            please refresh and try again. If this error persists,
            please contact support.'''
        return return_error(msg)

    if 'access_token' in r.json():
        session['api_key'] = r.json()['access_token']

        if 'refresh_token' in r.json():
            session['refresh_token'] = r.json()['refresh_token']

        if 'expires_in' in r.json():
            # expires in seconds
            # add the seconds to current time for expiration time
            current_time = int(time.time())
            expires_in = current_time + r.json()['expires_in']
            session['expires_in'] = expires_in

            # check if user is in the db
            user = Users.query.filter_by(user_id=int(session['canvas_user_id'])).first()
            if user is not None:
                # update the current user's expiration time in db
                user.refresh_token = session['refresh_token']
                user.expires_in = session['expires_in']
                db.session.add(user)
                db.session.commit()

                # check that the expires_in time got updated
                check_expiration = Users.query.filter_by(user_id=int(session['canvas_user_id']))

                # compare what was saved to the old session
                # if it didn't update, error
                if check_expiration.expires_in == long(session['expires_in']):
                    return redirect(url_for('index'))
                else:
                    app.logger.error(
                        '''Error in updating user's expiration time
                        in the db:\n {0}'''.format(session))
                    return return_error('''Authentication error,
                            please refresh and try again. If this error persists,
                            please contact Webcourses Support.''')
            else:
                # add new user to db
                new_user = Users(
                    session['canvas_user_id'],
                    session['refresh_token'],
                    session['expires_in']
                )
                db.session.add(new_user)
                db.session.commit()

                # check that the user got added
                check_user = Users.query.filter_by(user_id=int(session['canvas_user_id'])).first()

                if check_user is None:
                    # Error in adding user to the DB
                    app.logger.error(
                        "Error in adding user to db: \n {0}".format(session)
                    )
                    return return_error('''Authentication error,
                        please refresh and try again. If this error persists,
                        please contact Webcourses Support.''')
                else:
                    return redirect(url_for('index'))

            # got beyond if/else
            # error in adding or updating db

            app.logger.error(
                "Error in adding or updating user to db: \n {0} ".format(session)
            )
            return return_error('''Authentication error,
                please refresh and try again. If this error persists,
                please contact Webcourses Support.''')

    app.logger.warning(
        "Some other error\n {0} \n {1} \n Request headers: {2} \n {3}".format(
            session, r.url, r.headers, r.json()
        )
    )
    msg = '''Authentication error,
        please refresh and try again. If this error persists,
        please contact support.'''
    return return_error(msg)


@app.route('/launch', methods=['POST', 'GET'])
@check_valid_user
def launch():

    # if they aren't in our DB/their token is expired or invalid
    user = Users.query.filter_by(user_id=int(session['canvas_user_id'])).first()

    # Found a user
    if user is not None:
        # Get the expiration date
        expiration_date = user.expires_in
        refresh_token = user.refresh_key

        # If expired or no api_key
        if int(time.time()) > expiration_date or 'api_key' not in session:

            app.logger.info(
                '''Expired refresh token or api_key not in session\n
                User: {0} \n Expiration date in db: {1} {2}'''.format(
                    user.user_id, user.expires_in, session)
            )
            payload = {
                'grant_type': 'refresh_token',
                'client_id': settings.oauth2_id,
                'redirect_uri': settings.oauth2_uri,
                'client_secret': settings.oauth2_key,
                'refresh_token': refresh_token
            }
            r = requests.post(settings.BASE_URL+'login/oauth2/token', data=payload)

            # We got an access token and can proceed
            if 'access_token' in r.json():
                # Set the api key
                session['api_key'] = r.json()['access_token']
                app.logger.info(
                    "New access token created\n User: {0}".format(user.user_id)
                )
                if 'refresh_token' in r.json():
                    session['refresh_token'] = r.json()['refresh_token']

                if 'expires_in' in r.json():
                    # expires in seconds
                    # add the seconds to current time for expiration time
                    current_time = int(time.time())
                    expires_in = current_time + r.json()['expires_in']
                    session['expires_in'] = expires_in

                    # Try to save the new expiration date
                    user.expires_in = session['expires_in']
                    db.session.commit()

                    # check that the expiration date updated
                    check_expiration = Users.query.filter_by(
                        user_id=int(session['canvas_user_id'])).first()

                    # compare what was saved to the old session
                    # if it didn't update, error

                    if check_expiration.expires_in == long(session['expires_in']):
                        return redirect(url_for('index'))
                    else:
                        app.logger.error(
                            '''Error in updating user's expiration time
                             in the db:\n session: {0}'''.format(session)
                        )
                        return return_error('''Authentication error,
                            please refresh and try again. If this error persists,
                            please contact Webcourses Support.''')
            else:
                # weird response from trying to use the refresh token
                app.logger.info(
                    '''Access token not in json.
                    Bad api key or refresh token? {0} {1} {2} \n {3}'''.format(
                        r.status_code, session, payload, r.url
                    )
                )
                return return_error('''Authentication error,
                    please refresh and try again. If this error persists,
                    please contact Webcourses Support.''')
        else:
            # good to go!
            # test the api key
            auth_header = {'Authorization': 'Bearer ' + session['api_key']}
            r = requests.get(settings.API_URL + 'users/%s/profile' %
                             (session['canvas_user_id']), headers=auth_header)
            # check for WWW-Authenticate
            # https://canvas.instructure.com/doc/api/file.oauth.html
            if 'WWW-Authenticate' not in r.headers and r.status_code != 401:
                return redirect(url_for('index'))
            else:
                app.logger.info(
                    '''Reauthenticating: \n {0} \n {1} \n {2} \n {3}'''.format(
                        session, r.status_code, r.url, r.headers
                    )
                )
                return redirect(
                    settings.BASE_URL+'login/oauth2/auth?client_id=' +
                    settings.oauth2_id + '&response_type=code&redirect_uri=' +
                    settings.oauth2_uri
                )
            app.logger.error(
                '''Some other error: \n
                Session: {0} \n {1} \n {2} \n {3} \n {4}'''.format(
                    session, r.status_code,
                    r.url, r.headers, r.json()
                )
            )
            return return_error('''Authentication error,
                please refresh and try again. If this error persists,
                please contact Webcourses Support.''')
    else:
        # not in db, go go oauth!!
        app.logger.info(
            "Person doesn't have an entry in db, redirecting to oauth: {0}".format(session)
        )
        return redirect(settings.BASE_URL+'login/oauth2/auth?client_id='+settings.oauth2_id +
                        '&response_type=code&redirect_uri='+settings.oauth2_uri)

    app.logger.warning("Some other error, {0}".format(session))
    return return_error('''Authentication error, please refresh and try again. If this error persists,
        please contact Webcourses Support.''')


# ============================================
# XML
# ============================================

@app.route("/xml/", methods=['GET'])
def xml():
    """
    Returns the lti.xml file for the app.
    XML can be built at https://www.eduappcenter.com/
    """
    try:
        return Response(render_template(
            'lti.xml.j2'), mimetype='application/xml'
        )
    except:
        app.logger.error("No XML file.")
        msg = '''No XML file. Please refresh
            and try again. If this error persists,
            please contact support.'''
        return return_error(msg)
