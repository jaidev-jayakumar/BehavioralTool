import logging
from app import app

logger = logging.getLogger(__name__)

def handler(event, context):
    logger.debug("Lambda handler invoked")
    
    # Create a WSGI environment
    environ = {
        'wsgi.version': (1, 0),
        'wsgi.url_scheme': 'https',
        'wsgi.input': event['body'] if 'body' in event else '',
        'wsgi.errors': context.get_remaining_time_in_millis,
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        'httpMethod': event['httpMethod'],
        'SERVER_NAME': 'lambda',
        'SERVER_PORT': '443',
        'PATH_INFO': event['path'],
        'QUERY_STRING': event['queryStringParameters'] if 'queryStringParameters' in event else '',
        'REMOTE_ADDR': event['requestContext']['identity']['sourceIp'] if 'requestContext' in event and 'identity' in event['requestContext'] else '',
    }

    # Add HTTP headers
    for header, value in event.get('headers', {}).items():
        environ[f'HTTP_{header.replace("-", "_").upper()}'] = value

    # Create a function to start the response
    def start_response(status, response_headers, exc_info=None):
        return

    # Call the Flask application
    response = app(environ, start_response)

    # Convert the response to the format expected by API Gateway
    return {
        'statusCode': int(response[0].split()[0]),
        'headers': dict(response[1]),
        'body': b''.join(response[2]).decode('utf-8')
    }
