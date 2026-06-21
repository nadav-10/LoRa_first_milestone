from flask import Flask, render_template, request, make_response, redirect, url_for, session
from datetime import timedelta
import time
import copy

app = Flask(__name__)
app.secret_key = 'sa7f87saaviuoduiofrqu0s0fs'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=1)

class DummySimulator:
    def __init__(self):
        self.users_registery = {123 : DummyUser(123), 100 : DummyUser(100)}

    def run(self):
        while True:
            time.sleep(1)

class DummyUser:
    def __init__(self, key_id : int):
        self.fullname = "cat"
        self.nickname = "tree"
        self.phone = "051"
        self.key_id = key_id
        self.delay_secs = 5
        self.is_encrypted = False

@app.route('/')
def login_page():
    """
    Renders the dummy login page at the root URL (localhost:5000/).
    """
    return render_template('login.html')

@app.route('/mock-login', methods=['POST'])
def mock_login():
    """
    Sets a cookie to prove login success, then redirects to /home.
    """
    session.permanent = True
    session['key_id'] = 123
    return redirect(url_for('home_page'))

def _get_delay_color(delay_secs):
    if delay_secs <= 5:
        return "green"
    elif delay_secs <= 30:
        return "orange"
    else:
        return "red"
    
@app.route('/home')
def home_page():
    """
    Renders the placeholder home page (localhost:5000/home).
    """
    if not session.get('key_id') in sim.users_registery:
        return "<h1>Access Denied: Error 401</h1><p>You must log in first to view this page!</p>", 401

    current_user = sim.users_registery[session['key_id']]
    users = [user for user in sim.users_registery.values() if user.key_id != session['key_id']]
    # Pass the list and the color helper function straight to the HTML template
    return render_template('home.html', users=users, current_user=current_user, get_color=_get_delay_color)

@app.route('/update-profile', methods=['POST'])
def update_profile():
    if not session.get('key_id') in sim.users_registery:
        return "<h1>Access Denied</h1>", 401
        
    current_id = session['key_id']
    current_user = sim.users_registery[current_id]
    current_user.fullname = request.form.get('fullname')
    current_user.nickname = request.form.get('nickname')
    current_user.phone = request.form.get('phone')
    current_user.is_encrypted = 'is_encrypted' in request.form
    return redirect(url_for('home_page'))

if __name__ == '__main__':
    sim = DummySimulator()
    app.run(host='127.0.0.1', port=8080, debug=True)