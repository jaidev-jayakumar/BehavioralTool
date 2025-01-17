import os
from flask_migrate import Migrate
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from os import environ as env
from urllib.parse import quote_plus, urlencode
import stripe
from anthropic import Anthropic
import logging
from functools import wraps
from datetime import datetime, timedelta
import secrets
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # Replace with a secure secret key

# Database configuration
database_url = os.environ.get('DATABASE_URL')
if database_url is None:
    database_url = 'sqlite:///your_local_database.db'
elif database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_SECURE'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Instead of hardcoded keys
stripe.api_key = os.environ.get('STRIPE_API_KEY')
anthropic_client = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

# Configure Auth0
oauth = OAuth(app)
oauth.register(
    "auth0",
    client_id="CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U",
    client_secret="Xpsc111fF7ebg3gpCh9hr0v3LRBm7ADhT1w5z2oF0q270rmQlfxWjLpcCrtqXl_d",
    client_kwargs={
        "scope": "openid profile email",
    },
    server_metadata_url='https://auth.behai.ai/.well-known/openid-configuration'
)

# Database Models
class User(db.Model):
    id = db.Column(db.String, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    plan = db.Column(db.String(20), default='Starter')
    credits = db.Column(db.Integer, default=5)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, id, email):
        self.id = id
        self.email = email

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    plan = db.Column(db.String(20), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('transactions', lazy=True))

# Decorator to require authentication
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

@app.before_request
def redirect_www():
    urlparts = request.url.split('://')
    if urlparts[1].startswith('www.'):
        return redirect('https://' + urlparts[1][4:], code=301)

@app.route('/profile')
@requires_auth
def profile():
    user_id = session['user']['userinfo']['sub']
    user = User.query.get(user_id)
    if not user:
        flash("User not found.", "error")
        return redirect(url_for('home'))
    
    transactions = Transaction.query.filter_by(user_id=user_id).order_by(Transaction.timestamp.desc()).limit(5).all()
    
    return render_template('profile.html', 
                           session=session.get('user'),
                           user_role=user.plan,
                           remaining_credits=user.credits,
                           transactions=transactions)

def check_and_update_credits(user_id):
    user = User.query.get(user_id)
    if not user:
        return False
    
    if user.credits == -1:  # Unlimited plan
        return True
    elif user.credits > 0:
        user.credits -= 1
        db.session.commit()
        return True
    else:
        return False

@app.route('/')
def home():
    logged_in = 'user' in session
    payment_completed = session.get("payment_completed", False)
    
    if payment_completed:
        session.pop("payment_completed", None)  # Reset the payment_completed flag
    
    user_id = session.get("user", {}).get("userinfo", {}).get("sub")
    user = User.query.get(user_id) if user_id else None
    user_plan = user.plan if user else "Starter"
    remaining_credits = user.credits if user else 0
    
    return render_template('home.html', 
                           session=session.get('user'), 
                           logged_in=logged_in, 
                           payment_completed=payment_completed, 
                           plan=user_plan, 
                           remaining_credits=remaining_credits)

@app.route('/login')
def login():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    return oauth.auth0.authorize_redirect(
        redirect_uri=f"{request.scheme}://{request.host}/callback",
        state=state
    )

@app.route("/callback", methods=["GET", "POST"])
def callback():
    if 'oauth_state' not in session or session['oauth_state'] != request.args.get('state'):
        return "Invalid state parameter", 400
    
    try:
        token = oauth.auth0.authorize_access_token()
        app.logger.info(f"Token received: {token}")
        session["user"] = token
        
        user = User.query.get(token['userinfo']['sub'])
        if not user:
            user = User(id=token['userinfo']['sub'], email=token['userinfo']['email'])
            db.session.add(user)
            db.session.commit()
        
        checkout_action = session.pop('checkout_after_login', None)
        if checkout_action:
            return redirect(url_for('home', checkout_after_login=checkout_action))
        
        return redirect("/")
    except Exception as e:
        app.logger.error(f"Error in callback: {str(e)}")
        return f"An error occurred: {str(e)}", 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        "https://auth.behai.ai/v2/logout?" + urlencode(
            {
                "returnTo": "https://behai.ai/",
                "client_id": "CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U",
            },
            quote_via=quote_plus,
        )
    )

@app.route('/analyze', methods=['POST'])
@requires_auth
def analyze():
    user_id = session['user']['userinfo']['sub']
    if not check_and_update_credits(user_id):
        flash("You've used all your credits. Please upgrade your plan for more.", "error")
        return redirect(url_for('home'))

    experience_text = request.form['experience']
    company_blurb = request.form['company_blurb']
    role = request.form['role']
    question = request.form['question']

    if not all([experience_text, company_blurb, role, question]):
        flash("Please fill in all fields.", "error")
        return redirect(url_for('home'))

    try:
        answer = generate_answer(question, experience_text, company_blurb, role)
        return redirect(url_for('result', answer=answer, question=question, 
                                experience_text=experience_text, company_blurb=company_blurb, role=role))
    except Exception as e:
        flash(f"An error occurred: {str(e)}", "error")
        return redirect(url_for('home'))

@app.route('/result')
@requires_auth
def result():
    return render_template('result.html', 
                           answer=request.args.get('answer'),
                           question=request.args.get('question'),
                           experience_text=request.args.get('experience_text'),
                           company_blurb=request.args.get('company_blurb'),
                           role=request.args.get('role'))

@app.route('/update_question', methods=['POST'])
@requires_auth
def update_question():
    user_id = session['user']['userinfo']['sub']
    if not check_and_update_credits(user_id):
        return jsonify(error="You've used all your credits. Please upgrade your plan for more.", redirect="/"), 403

    question = request.json['question']
    experience_text = request.json['experience_text']
    company_blurb = request.json['company_blurb']
    role = request.json['role']
    
    try:
        answer = generate_answer(question, experience_text, company_blurb, role)
        return jsonify(answer=answer)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route('/create-checkout-session', methods=['POST'])
@requires_auth
def create_checkout_session():
    try:
        is_unlimited_plan = request.args.get('unlimited', 'false').lower() == 'true'
        
        if is_unlimited_plan:
            plan_name = 'All-In'
            amount = 999  # $9.99
        else:
            plan_name = 'Pro'
            amount = 599  # $5.99

        user_id = session['user']['userinfo']['sub']
        user_email = session['user']['userinfo']['email']

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'Behai.ai {plan_name} Plan',
                    },
                    'unit_amount': amount,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url="https://behai.ai/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url="https://behai.ai/cancel",
            client_reference_id=user_id,
            customer_email=user_email,
            metadata={
                'plan_name': plan_name
            }
        )
        return jsonify({'sessionId': checkout_session['id']})
    except Exception as e:
        app.logger.error(f"Error creating checkout session: {str(e)}")
        return jsonify({'error': str(e)}), 403

@app.route('/success')
def success():
    session_id = request.args.get('session_id')
    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            if checkout_session.payment_status == 'paid':
                user_id = checkout_session.client_reference_id
                plan_name = checkout_session.metadata.get('plan_name')
                
                user = User.query.get(user_id)
                if user:
                    user.plan = plan_name
                    if plan_name == 'All-In':
                        user.credits = -1  # Unlimited credits
                    elif plan_name == 'Pro':
                        user.credits = 20
                    
                    transaction = Transaction(user_id=user_id, amount=checkout_session.amount_total / 100, plan=plan_name)
                    db.session.add(transaction)
                    db.session.commit()
                
                flash("Your payment was successful! Your plan has been upgraded.", "success")
            else:
                flash("Payment was not completed. Please try again or contact support.", "error")
        except Exception as e:
            app.logger.error(f"Error processing successful payment: {str(e)}")
            flash("An error occurred while processing your payment. Please contact support.", "error")
    else:
        flash("No checkout session found. If you've made a payment, please contact support.", "warning")
    
    return redirect(url_for('home'))

@app.route('/cancel')
def cancel():
    flash("Your payment was cancelled.", "info")
    return render_template('cancel.html')

def generate_answer(question, experience_text, company_blurb, role):
    # Create the message content
    message_content = f"""Generate a detailed, compelling behavioral answer in the STAR format (Situation, Task, Action, Result) for the following question based on the candidate's provided experience, company blurb, and role. Focus on the specific experiences, skills, and impact mentioned in the candidate's input. Provide relevant examples and quantify results where possible, ensuring that the answer is consistent with the given experience, company, and role.

For questions about weaknesses, challenges, conflicts, or failures, frame the response to demonstrate self-awareness, problem-solving skills, personal growth, and lessons learned. Ensure the answer addresses the specific question asked, even if it requires discussing areas for improvement.

Question: {question}

Candidate's Experience: {experience_text}

Company Blurb: {company_blurb}

Role: {role}

Please follow this format:

Situation: [Describe a specific situation or context from the candidate's experience that is relevant to the question and role at the mentioned company]

Task: [Explain the task, challenge, or responsibility the candidate needed to address in that situation while working in the specified role]

Action: [Detail the specific actions the candidate took to address the situation, leveraging their skills and experiences from the provided experience. For questions about weaknesses or challenges, focus on steps taken to improve or overcome them.]

Result: [Highlight the measurable outcomes or impact achieved through the candidate's actions, using metrics or examples from the candidate's experience where applicable. For questions about weaknesses or challenges, emphasize growth, lessons learned, or improvements made.]"""

    # Create the message using the Messages API
    message = anthropic_client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=400,
        messages=[
            {
                "role": "user",
                "content": message_content
            }
        ]
    )

    # Return the response content
    return message.content[0].text

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)