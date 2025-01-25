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

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'your_secret_key_here')

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

# API keys from environment variables
stripe.api_key = os.environ.get('STRIPE_API_KEY')
anthropic_client = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

# Configure Auth0
oauth = OAuth(app)
oauth.register(
    "auth0",
    client_id=os.environ.get('AUTH0_CLIENT_ID', "CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U"),
    client_secret=os.environ.get('AUTH0_CLIENT_SECRET', "Xpsc111fF7ebg3gpCh9hr0v3LRBm7ADhT1w5z2oF0q270rmQlfxWjLpcCrtqXl_d"),
    client_kwargs={
        "scope": "openid profile email",
    },
    server_metadata_url='https://auth.behai.ai/.well-known/openid-configuration'
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

@app.route('/')
def home():
    logged_in = 'user' in session
    payment_completed = session.get("payment_completed", False)
    
    if payment_completed:
        session.pop("payment_completed", None)
    
    user_id = session.get("user", {}).get("userinfo", {}).get("sub")
    user = User.query.get(user_id) if user_id else None
    user_plan = user.plan if user else "Starter"
    remaining_credits = user.credits if user else 0
    
    return render_template('home.html', 
                         session=session.get('user'), 
                         logged_in=logged_in, 
                         payment_completed=payment_completed, 
                         plan=user_plan, 
                         remaining_credits=remaining_credits,
                         checkout_after_login=request.args.get('checkout_after_login'))

@app.route('/login')
def login():
    state = secrets.token_urlsafe(16)
    session['oauth_state'] = state
    checkout_action = request.args.get('action')
    if checkout_action:
        session['checkout_after_login'] = checkout_action
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
        logger.info(f"Token received: {token}")
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
        logger.error(f"Error in callback: {str(e)}")
        return f"An error occurred: {str(e)}", 500

@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        "https://auth.behai.ai/v2/logout?" + urlencode(
            {
                "returnTo": "https://behai.ai/",
                "client_id": os.environ.get('AUTH0_CLIENT_ID', "CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U"),
            },
            quote_via=quote_plus,
        )
    )

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
        logger.error(f"Error in analyze: {str(e)}")
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
        logger.error(f"Error in update_question: {str(e)}")
        return jsonify(error=str(e)), 500

@app.route('/generate-email', methods=['POST'])
@requires_auth
def generate_email():
    user_id = session['user']['userinfo']['sub']
    if not check_and_update_credits(user_id):
        return jsonify(error="You've used all your credits. Please upgrade your plan for more."), 403

    data = request.json
    target_company = data.get('target_company')
    target_role = data.get('target_role')
    experience = data.get('experience')
    recipient_name = data.get('recipient_name', '')

    if not all([target_company, target_role, experience]):
        return jsonify(error="Please fill in all required fields."), 400

    try:
        message_content = f"""Generate a cold email using exactly this format:

Start with:
"Hi {recipient_name if recipient_name else ''},

My name is *Insert name*. I understand your time is valuable, I'll only write three bullet points."

Then generate 3 distinct bullet points that:
1. First bullet: Focus on your highest-level role/education/credential that matches the target role
2. Second bullet: Highlight your most impressive quantified achievement or scale of impact
3. Third bullet: Showcase a unique skill, approach, or mindset that sets you apart

Format rules for bullets:
- Use **bold** for key terms, metrics, and company names
- Each bullet should focus on ONE key thing (not multiple achievements)
- Transform complex achievements into simple, impactful statements
- Avoid repeating the same metrics or achievements
- Keep each bullet under 20 words

For example, turn this experience:
"Led healthcare provider launches worth $2M+ in take rate as Operations Lead, optimizing onboarding and integration across 50+ implementations"

Into distinct bullets like:
"- **Operations Lead** with track record of scaling healthcare technology solutions at **high-growth startups**
- Drove **$2M+ revenue growth** through strategic provider partnerships and optimized implementations
- Proven ability to build and lead **cross-functional teams** while maintaining operational excellence"

End with exactly:
"Interested in the **{target_role}** role at **{target_company}**. My apologies for the cold outreach but I have been very interested in the work going on at {target_company} and I'd be thrilled to have an opportunity to interview for this role."

Use these details to generate the bullet points:
Role: {target_role}
Company: {target_company}
Experience: {experience}"""

        message = anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": message_content
                }
            ]
        )
        
        return jsonify({'email': message.content[0].text})
    except Exception as e:
        logger.error(f"Error generating email: {str(e)}")
        return jsonify({'error': str(e)}), 500

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
        logger.error(f"Error creating checkout session: {str(e)}")
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
                    
                    transaction = Transaction(
                        user_id=user_id, 
                        amount=checkout_session.amount_total / 100, 
                        plan=plan_name
                    )
                    db.session.add(transaction)
                    db.session.commit()
                
                flash("Your payment was successful! Your plan has been upgraded.", "success")
            else:
                flash("Payment was not completed. Please try again or contact support.", "error")
        except Exception as e:
            logger.error(f"Error processing successful payment: {str(e)}")
            flash("An error occurred while processing your payment. Please contact support.", "error")
    else:
        flash("No checkout session found. If you've made a payment, please contact support.", "warning")
    
    return redirect(url_for('home'))

@app.route('/optimize-resume', methods=['POST'])
@requires_auth
def optimize_resume():
    user_id = session['user']['userinfo']['sub']
    if not check_and_update_credits(user_id):
        return jsonify(error="You've used all your credits. Please upgrade your plan for more."), 403

    data = request.json
    job_description = data.get('job_description')
    experiences = data.get('experiences', [])

    try:
        message_content = f"""Optimize these resume bullet points specifically for this job description while maintaining a natural, human voice.

Key Guidelines:
1. Use the X-Y-Z formula naturally: "Accomplished [X] as measured by [Y], by doing [Z]"
2. Quantify impact and show growth wherever possible
3. Match relevant skills/keywords from the job description
4. Write in a conversational, authentic tone
5. Keep each bullet concise but impactful

Style Requirements:
- Transform technical achievements into compelling personal stories
- Include metrics naturally within the narrative
- Use strong action verbs while maintaining conversational flow
- Focus on your direct impact while acknowledging team context
- Keep each bullet to one line where possible

Examples of Natural Transformation:
Original: "Led healthcare provider launches worth $2M+"
Better: "Orchestrated company's largest market expansion, driving end-to-end execution worth $2M+ in revenue while reimagining the provider experience"

Original: "Built scalable framework"
Better: "Shaped the team's growth journey from 3 to 20+ members by implementing an adaptable operational framework that maintained our quality standards"

Original: "Implemented a new marketing strategy"
Optimized: "Transformed marketing approach, increasing lead conversion rates by 35% through targeted campaign strategies."


Job Description: {job_description}
Experiences to Optimize: {experiences}

Make each bullet point sound like someone naturally describing their real achievements in a compelling way, not following a rigid template."""

        message = anthropic_client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=500,
            messages=[{"role": "user", "content": message_content}]
        )
        
        return jsonify({'optimized_bullets': message.content[0].text})
    except Exception as e:
        logger.error(f"Error optimizing resume: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/cancel')
def cancel():
    flash("Your payment was cancelled.", "info")
    return redirect(url_for('home'))

def generate_answer(question, experience_text, company_blurb, role):
    message_content = f"""Generate a conversational, compelling behavioral answer in the STAR format that flows naturally like someone speaking in an interview. The response should feel authentic and engaging, not like a list of bullet points.

Question: {question}

Candidate's Experience: {experience_text}

Company & Role Context:
- Company: {company_blurb}
- Role: {role}

Guidelines for the response:
1. Structure using STAR but make it flow naturally:
   - Situation: Set the scene with context and background
   - Task: Explain the specific challenge or objective
   - Action: Describe what you did (use first person, active voice)
   - Result: Share the impact and outcomes

2. Style requirements:
   - Write in a natural, conversational tone
   - Avoid bullet points or lists
   - Connect ideas with smooth transitions
   - Include specific details and metrics but weave them naturally into sentences
   - Use confident but humble language
   - Keep paragraphs short and digestible
   - For questions about challenges/weaknesses, show growth and self-awareness

Example tone (not content to copy):
"In my previous role at HealthTech, we were facing a critical moment in our growth phase. Our provider onboarding process was becoming a bottleneck as we scaled, and I recognized we needed a complete overhaul of our approach. I took ownership of this challenge and started by deeply analyzing our existing implementations. What I found was eye-opening - each team was essentially creating their own process, leading to inconsistent results and frustrated providers. 

I knew we needed a standardized playbook, but I wanted to build it from real data and experience. I spent three weeks interviewing our top-performing team members, documenting their best practices, and mapping out the common pitfalls they'd encountered. Using these insights, I developed a comprehensive implementation framework that any team member could follow.

The results were transformative. Not only did we successfully onboard 50+ new providers using this framework, but we also saw our customer satisfaction scores shift from neutral to consistently positive. What I'm most proud of though, is how this framework enabled us to scale our team from 3 to over 20 members in just two months while maintaining quality. We ended up generating $2M in additional revenue through these streamlined launches."

For this specific question and experience, please create a natural, flowing response that demonstrates the candidate's capabilities while maintaining an authentic, conversational tone."""

    try:
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
        return message.content[0].text
    except Exception as e:
        logger.error(f"Error generating answer: {str(e)}")
        raise

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    return render_template('500.html'), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=os.environ.get('FLASK_DEBUG', 'True').lower() == 'true')