# Copyright 2019 Ben Kehoe
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import base64
import datetime
import os
import uuid
import sys
import traceback
from collections import namedtuple

import dateutil.parser
import boto3
import botocore.exceptions
import aws_encryption_sdk
import jsonschema

from sfn_callback_urls.payload import PayloadBuilder, encode_payload
from sfn_callback_urls.callbacks import get_url
from sfn_callback_urls.common import send_log_event, RequestError

class MissingApiParametersError(RequestError):
    pass

class InvalidActionError(RequestError):
    pass

class InvalidDateError(RequestError):
    pass

"""
EVENT EXAMPLE
{
    'token': '',
    'expiration': '',
    'actions': {
        'name1': {
            'type': 'success',
            'output': {},
        },
        'name2': {
            'type': 'failure',
            'error': '',
            'cause': '',
        },
        'name3': {
            'type': 'heartbeat',
        }
    },
    'enable_output_parameters': False,
    'api': {
        'api_id': '',
        'stage': '',
        'region': '',
    }
}
"""

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["success", "failure", "heartbeat"]
        },
    },
    "required": ["type"],
    "allOf": [
        {
            "if": {
                "properties": { "type": { "const": "success" } }
            },
            "then": {
                "properties": {
                    "output": {
                        "type": "object"
                    }
                },
                "required": ["output"]
            }
            
        },
        {
            "if": {
                "properties": { "type": { "const": "failure" } }
            },
            "then": {
                "properties": {
                    "error": {
                        "type": "string"
                    },
                    "reason": {
                        "type": "string"
                    }
                }
            }
        }
    ]
}

CREATE_URL_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "token": {
            "type": "string"
        },
        "expiration": {
            "type": "string",
            "format": "date-time"
        },
        "actions": {
            "type": "object",
            "additionalProperties": False,
            "patternProperties": {
                "^\\w+$": ACTION_SCHEMA,
            },
            "minProperties": 1,
        },
        "enable_output_parameters": {
            "type": "boolean"
        },
        "api": {
            "type": "object",
            "properties": {
                "api_id": {
                    "type": "string"
                },
                "stage": {
                    "type": "string"
                },
                "region": {
                    "type": "string",
                }
            },
            "required": ["api_id", "stage"]
        }
    },
    "required": ["token", "actions"],
    "additionalProperties": False
}

BOTO3_SESSION = boto3.Session()
MASTER_KEY_PROVIDER = None
if 'KEY_ID' in os.environ:
    MASTER_KEY_PROVIDER = aws_encryption_sdk.KMSMasterKeyProvider(
        key_ids = [os.environ['KEY_ID']],
        botocore_session = BOTO3_SESSION._session
    )

def get_header(event, name):
    for key in event['headers']:
        if key.lower() == name.lower():
            return event['headers'][key]
    return None

DefaultApiInfo = namedtuple('DefaultApiInfo', ['region', 'api_id', 'stage'])

def direct_handler(event, context):
    default_api_info = DefaultApiInfo(
        region=BOTO3_SESSION.region_name,
        api_id=os.environ['API_ID'],
        stage=os.environ['STAGE']
    )

    def response_formatter(statusCode, response):
        return response
    
    return process_event(event, context, default_api_info, response_formatter)

def api_handler(event, context):
    default_api_info = DefaultApiInfo(
        region=BOTO3_SESSION.region_name,
        api_id=event['requestContext']['apiId'],
        stage=event['requestContext']['stage']
    )

    def response_formatter(statusCode, response):
        return {
            'statusCode': statusCode,
            'headers': {
                'Content-Type': 'application/json',
            },
            'body': json.dumps(response)
        }

    if event['httpMethod'] != 'POST':
        return {
            'statusCode': 405,
            'headers': {
                'Allow': 'POST'
            }
        }
    
    if get_header(event, 'content-type') != 'application/json':
        return {
            'statusCode': 415,
            'headers': {

            }
        }
    
    try:
        event = json.loads(event['body'])
    except json.JSONDecodeError as e:
        return {
            'statusCode': 400,
            'headers': {
                'Content-Type': 'application/json',
            },
            'body': json.dumps({
                'error': 'InvalidJSON',
                'message': f'{str(e)}',
            })
        }
    
    return process_event(event, context, default_api_info, response_formatter)

def process_event(event, context, default_api_info, response_formatter):
    print(f'Received event: {event}')
        
    try:
        jsonschema.validate(event, CREATE_URL_EVENT_SCHEMA)
    except jsonschema.ValidationError as e:
        return response_formatter(400,{
                    'error': 'InvalidJSON',
                    'message': f'{str(e)}',
                })

    transaction_id = uuid.uuid4().hex
    timestamp = datetime.datetime.now()

    log_event = {
        'transaction_id': transaction_id,
        'timestamp': timestamp.isoformat(),
        'actions': [],
    }

    try:
        if 'api' in event:
            api_spec = event['api']
            region = api_spec.get('region', default_api_info.region)
            api_id = api_spec.get('api_id')
            stage = api_spec.get('stage')

            missing = []
            if not api_id:
                missing.append('API id')
            if not stage:
                missing.append('stage')
            if missing:
                message = 'Missing ' + ' and '.join(missing)
                raise MissingApiParametersError(message)
        else:
            region = default_api_info.region
            api_id = default_api_info.api_id
            stage = default_api_info.stage
        
        log_event.update({
            'api_id': api_id,
            'stage': stage,
            'region': region,
        })

        response = {
            'transaction_id': transaction_id,
            'urls': {},
        }
        
        expiration = None
        if 'expiration' in event:
            try:
                expiration = dateutil.parser.parse(event['expiration'])
            except Exception as e:
                raise InvalidDateError(f'Invalid expiration: {str(e)}')
            expiration_delta = (expiration - timestamp).total_seconds()
            if expiration_delta <= 0:
                raise InvalidDateError('Expiration is in the past')
            log_event['expiration_delta'] = expiration_delta
            response['expiration'] = expiration.isoformat()
        
        payload_builder = PayloadBuilder(transaction_id, timestamp, event['token'],
                enable_output_parameters=event.get('enable_output_parameters'),
                expiration=expiration)

        actions = {}
        for action_name, action_data in event['actions'].items():
            action_type = action_data['type']
            actions[action_name] = action_type
            payload_data = {}
            
            if action_type == 'success':
                payload_data['output'] = action_data['output']
            elif action_type == 'failure':
                for key in ['error', 'cause']:
                    if key in action_data:
                        payload_data[key] = action_data[key]
            elif action_type != 'heartbeat':
                raise InvalidActionError(f'Unexpected action type {action_type}')

            payload = payload_builder.build(action_name, action_type, payload_data,
                    log_event=log_event)

            encoded_payload = encode_payload(payload, MASTER_KEY_PROVIDER)

            response['urls'][action_name] = get_url(
                    action_name, action_type, encoded_payload,
                    api_id, stage, region, log_event=log_event)

        log_event['actions'] = actions
        
        print(f'Sending response: {json.dumps(response)}')

        send_log_event(log_event)

        return response_formatter(200, response)
    except RequestError as e:
        response = {
            'transaction_id': transaction_id,
            'error': e.code(),
            'message': e.message(),
        }
        log_event['error'] = {
            'type': 'RequestError',
            'error': e.code(),
            'message': e.message(),
        }
        send_log_event(log_event)
        return response_formatter(400, response)
    except Exception as e:
        traceback.print_exc()
        error_class_name = type(e).__module__ + '.' + type(e).__name__
        response = {
            'error': 'ServiceError',
            'message': f'{error_class_name}: {str(e)}'
        }
        log_event['error'] = {
            'type': 'Unexpected',
            'error': error_class_name,
            'message': str(e),
        }
        send_log_event(log_event)
        return response_formatter(500, response)
