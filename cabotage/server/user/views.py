from flask import render_template, Blueprint
from flask_security import login_required

user_blueprint = Blueprint('user', __name__,)


@user_blueprint.route('/members')
@login_required
def members():
    return render_template('user/members.html')
