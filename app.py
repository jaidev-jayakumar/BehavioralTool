from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import stripe
import anthropic
import logging
import json
from os import environ as env
from urllib.parse import quote_plus, urlencode
from authlib.integrations.flask_client import OAuth
from dotenv import find_dotenv, load_dotenv

app = Flask(__name__)
app.secret_key = 'b6527e007ea0e41032f93e5facc4543ecaa947553013efc79694c03a08eb59c9'


ENV_FILE = find_dotenv('text.env')
if ENV_FILE:
    load_dotenv(ENV_FILE)

# Configure Stripe
stripe.api_key = "sk_test_51P2NaoRsyNEomGjpR1XrqWhHuKIYgYYCNjH4F77QrKoBs0KJ875Ag286Pt6SZUCEDuSGy84PwBpPPHdTacDmPy9b00PTJBJy0S"

claude = anthropic.Client(api_key="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")

oauth = OAuth(app)
oauth.register(
    "auth0",
    client_id="CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U",
    client_secret="Xpsc111fF7ebg3gpCh9hr0v3LRBm7ADhT1w5z2oF0q270rmQlfxWjLpcCrtqXl_d",
    client_kwargs={
        "scope": "openid profile email",
    },
    server_metadata_url='https://dev-jb8yhreazf12vlqi.us.auth0.com/.well-known/openid-configuration'
)


def generate_answer(question, experience_text, company_blurb, role, retries=3):
    for attempt in range(retries):
        try:
            prompt = f"{anthropic.HUMAN_PROMPT}Generate a detailed, compelling behavioral answer in the STAR format (Situation, Task, Action, Result) for the following question based on the candidate's provided experience, company blurb, and role. Focus on the specific experiences, skills, and impact mentioned in the candidate's input. Provide relevant examples and quantify results where possible, ensuring that the answer is consistent with the given experience, company, and role.\n\nQuestion: {question}\n\nCandidate's Experience: {experience_text}\n\nCompany Blurb: {company_blurb}\n\nRole: {role}\n\nPlease follow this format:\n\nSituation: [Describe a specific situation or context from the candidate's experience that is relevant to the question and role at the mentioned company]\n\nTask: [Explain the task, challenge, or responsibility the candidate needed to address in that situation while working in the specified role]\n\nAction: [Detail the specific actions the candidate took to address the situation, leveraging their skills and experiences from the provided experience]\n\nResult: [Highlight the measurable outcomes or impact achieved through the candidate's actions, using metrics or examples from the candidate's experience where applicable]\n\nEnsure that the answer is consistent with the provided experience, company blurb, and role, and does not mention any other companies or roles not specified by the user.\n\n{anthropic.AI_PROMPT}"

            response = claude.completions.create(
                prompt=prompt,
                stop_sequences=["\n\nHuman:"],
                model="claude-v1",
                max_tokens_to_sample=400
            )

            return response.completion.strip()

        except anthropic.APIError as e:
            logging.error(f"Anthropic API Error: {str(e)}")
            if attempt < retries - 1:
                delay = 2 ** attempt
                logging.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logging.error(f"An error occurred: {str(e)}")
            if attempt < retries - 1:
                delay = 2 ** attempt
                logging.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                raise
def check_plan_and_remaining_credits(user_id, user_plan):
    # Retrieve the user's remaining credits from the session
    remaining_credits = session.get(f"{user_id}_remaining_credits")

    # If the remaining credits are not set in the session, initialize them based on the user's plan
    if remaining_credits is None:
        if user_plan == 'Starter':
            remaining_credits = 5
        elif user_plan == 'Pro':
            remaining_credits = 20
        else:
            remaining_credits = -1  # -1 indicates unlimited credits

        # Store the remaining credits in the session
        session[f"{user_id}_remaining_credits"] = remaining_credits

    return remaining_credits

@app.route('/')
def home():
    logged_in = session.get("logged_in", False)
    payment_completed = session.get("payment_completed", False)
    
    if payment_completed:
        session.pop("payment_completed", None)  # Reset the payment_completed flag
    
    user_id = session.get("user", {}).get("userinfo", {}).get("sub")
    user_plan = session.get("plan", "Starter")
    remaining_credits = check_plan_and_remaining_credits(user_id, user_plan)
    
    return render_template('home.html', session=session.get('user'), logged_in=logged_in, payment_completed=payment_completed, plan=user_plan, remaining_credits=remaining_credits)


@app.route('/login')
def login():
    return oauth.auth0.authorize_redirect(
        redirect_uri=url_for("callback", _external=True)
    )

@app.route("/callback", methods=["GET", "POST"])
def callback():
    token = oauth.auth0.authorize_access_token()
    session["user"] = token
    session["logged_in"] = True  # Set a flag indicating successful login
    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(
        "https://dev-jb8yhreazf12vlqi.us.auth0.com/v2/logout?" + urlencode(
            {
                "returnTo": url_for("home", _external=True),
                "client_id": "CCu9ZpI4SUJbP0N0dpvrumvaetYyZh8U",
            },
            quote_via=quote_plus,
        )
    )


@app.route('/analyze', methods=['GET', 'POST'])
def analyze():
    if request.method == 'POST':
        experience_text = request.form['experience']
        company_blurb = request.form['company_blurb']
        role = request.form['role']
        question = request.form['question']

        if not experience_text or not company_blurb or not role or not question:
            return "Please fill in all fields."

        user_id = session.get("user", {}).get("userinfo", {}).get("sub")
        user_plan = session.get("plan", "Starter")
        remaining_credits = check_plan_and_remaining_credits(user_id, user_plan)

        if remaining_credits == 0 and user_plan != 'All-In':
            return "You have reached the maximum number of credits for your plan."

        try:
            answer = generate_answer(question, experience_text, company_blurb, role)
            session['experience_text'] = experience_text
            session['company_blurb'] = company_blurb
            session['role'] = role

            # Decrement the remaining credits if the user's plan is not 'All-In'
            if user_plan != 'All-In':
                session[f"{user_id}_remaining_credits"] -= 1

            remaining_credits = session[f"{user_id}_remaining_credits"]
            return render_template('result.html', answer=answer, question=question, experience_text=experience_text, company_blurb=company_blurb, role=role, remaining_credits=remaining_credits)
        except Exception as e:
            return f"An error occurred: {str(e)}"

    return render_template('analyze.html')

@app.route('/create-checkout-session', methods=['POST'])
def create_checkout_session():
    try:
        # Check if the request is for the "All-In" plan
        is_unlimited_plan = request.args.get('unlimited', False)

        line_items = []
        if is_unlimited_plan:
            line_items = [
                {
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': 'Behavioraly.ai All-In Plan',
                        },
                        'unit_amount': 999,  # Amount in cents ($9.99 USD)
                    },
                    'quantity': 1,
                },
            ]
            session['plan'] = 'All-In'
        else:
            line_items = [
                {
                    'price_data': {
                        'currency': 'usd',
                        'product_data': {
                            'name': 'Behavioraly.ai Pro Plan',
                        },
                        'unit_amount': 599,  # Amount in cents ($5.99 USD)
                    },
                    'quantity': 1,
                },
            ]
            session['plan'] = 'Pro'

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=line_items,
            mode='payment',
            success_url=request.host_url + 'success',
            cancel_url=request.host_url + 'cancel',
        )
        return jsonify({'sessionId': checkout_session['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 403

@app.route('/success')
def success():
    session["payment_completed"] = True  # Set a flag indicating payment completion
    return render_template('success.html')

@app.route('/cancel')
def cancel():
    # Handle canceled payment logic here
    return render_template('cancel.html')

@app.route('/update_question', methods=['POST'])
def update_question():
    question = request.json['question']
    experience_text = request.json['experience_text']
    company_blurb = request.json['company_blurb']
    role = request.json['role']
    answer = generate_answer(question, experience_text, company_blurb, role)
    return jsonify(answer=answer)

if __name__ == '__main__':
    app.run(debug=True)