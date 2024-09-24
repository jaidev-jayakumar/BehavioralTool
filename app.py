import os
from flask_migrate import Migrate
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from authlib.integrations.flask_client import OAuth
from os import environ as env
from urllib.parse import quote_plus, urlencode
import stripe
import anthropic
import logging
from functools import wraps
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'  # Replace with a secure secret key

from flask import request, redirect

@app.before_request
def redirect_www():
    urlparts = request.url.split('://')
    if urlparts[1].startswith('www.'):
        return redirect('https://' + urlparts[1][4:], code=301)

# Database configuration
database_url = os.environ.get('DATABASE_URL')
if database_url is None:
    # Use a local SQLite database for development
    database_url = 'sqlite:///your_local_database.db'
elif database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# Configure Stripe
stripe.api_key = "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"

# Configure Anthropic
claude = anthropic.Client(api_key="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")

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
    action = request.args.get('action')
    if action in ['pro', 'unlimited']:
        session['checkout_after_login'] = action
    
    return oauth.auth0.authorize_redirect(
        redirect_uri=f"{request.scheme}://{request.host}/callback"
    )

@app.route("/callback", methods=["GET", "POST"])
def callback():
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
        "https://dev-jb8yhreazf12vlqi.us.auth0.com/v2/logout?" + urlencode(
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
        return jsonify(error="You've reached your credit limit. Please upgrade your plan."), 403

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
    
    # Redirect to home page instead of rendering success.html
    return redirect(url_for('home'))

@app.route('/cancel')
def cancel():
    flash("Your payment was cancelled.", "info")
    return render_template('cancel.html')

def generate_answer(question, experience_text, company_blurb, role):
    prompt = f"{anthropic.HUMAN_PROMPT}Generate a detailed, compelling behavioral answer in the STAR format (Situation, Task, Action, Result) for the following question based on the candidate's provided experience, company blurb, and role. Focus on the specific experiences, skills, and impact mentioned in the candidate's input. Provide relevant examples and quantify results where possible, ensuring that the answer is consistent with the given experience, company, and role.\n\nQuestion: {question}\n\nCandidate's Experience: {experience_text}\n\nCompany Blurb: {company_blurb}\n\nRole: {role}\n\nPlease follow this format:\n\nSituation: [Describe a specific situation or context from the candidate's experience that is relevant to the question and role at the mentioned company]\n\nTask: [Explain the task, challenge, or responsibility the candidate needed to address in that situation while working in the specified role]\n\nAction: [Detail the specific actions the candidate took to address the situation, leveraging their skills and experiences from the provided experience]\n\nResult: [Highlight the measurable outcomes or impact achieved through the candidate's actions, using metrics or examples from the candidate's experience where applicable]\n\nEnsure that the answer is consistent with the provided experience, company blurb, and role, and does not mention any other companies or roles not specified by the user.\n\n{anthropic.AI_PROMPT}"

    response = claude.completions.create(
        prompt=prompt,
        stop_sequences=["\n\nHuman:"],
        model="claude-v1",
        max_tokens_to_sample=400
    )

    return response.completion.strip()

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)