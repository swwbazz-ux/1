"""Ограниченный FCM-пилот одного USB-получателя; без доступа к БД/серверу."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROJECT = 'copper-resources-driver'
TARGET_SHA256 = '4f14ed20bb3369faa91680857223e3e34be5d6ca5861d65a3968959d01bcf9b7'
CONTEXT = b'copper-fcm-v2-pilot-99-test1'


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def target_token(account, envelope):
    if account.get('project_id') != PROJECT:
        raise ValueError('Unexpected Firebase project')
    private = serialization.load_pem_private_key(account['private_key'].encode(), password=None)
    wrapped = base64.b64decode(envelope['keys'][account['private_key_id']], validate=True)
    key = private.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    token = AESGCM(key).decrypt(base64.b64decode(envelope['nonce'], validate=True), base64.b64decode(envelope['ciphertext'], validate=True), CONTEXT).decode()
    if hashlib.sha256(token.encode()).hexdigest() != TARGET_SHA256:
        raise ValueError('Only the verified USB target is allowed')
    return token


def oauth(account):
    now = int(time.time())
    body = b64(json.dumps({'alg':'RS256','typ':'JWT'}, separators=(',',':')).encode()) + '.' + b64(json.dumps({'iss':account['client_email'],'scope':'https://www.googleapis.com/auth/firebase.messaging','aud':'https://oauth2.googleapis.com/token','iat':now,'exp':now+600}, separators=(',',':')).encode())
    private = serialization.load_pem_private_key(account['private_key'].encode(), password=None)
    assertion = body + '.' + b64(private.sign(body.encode(),padding.PKCS1v15(),hashes.SHA256()))
    request = urllib.request.Request('https://oauth2.googleapis.com/token', data=urllib.parse.urlencode({'grant_type':'urn:ietf:params:oauth:grant-type:jwt-bearer','assertion':assertion}).encode(), headers={'Content-Type':'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(request,timeout=15) as response:
        return json.load(response)['access_token']


def payload(token, phase, probe_id):
    message={'token':token,'data':{'kind':'fcm_latency_probe','version':'pilot','probe_id':probe_id}}
    if phase == 'legacy':
        message['android']={'priority':'high','ttl':'120s'}
    elif phase in ('validate','urgent'):
        message['android_v2']={'ttl':'120s','remote_notification':{'mutable_content':True,'use_as_v1_data_message':True,'notification':{'title':'Проверка связи','body':'Получен тестовый сигнал','tag':'fcm-urgent-pilot','id':1299}}}
    else:
        raise ValueError('Unknown pilot phase')
    return {'validate_only':phase=='validate','message':message}


def main():
    if os.environ.get('PILOT_CONFIRMATION') != 'FCM_PILOT_99_TEST1':
        raise ValueError('Explicit pilot confirmation is required')
    if time.time() > datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc).timestamp():
        raise ValueError('The bounded pilot window has expired')
    phase=os.environ['PILOT_PHASE']
    if phase not in ('validate','legacy','urgent'):
        raise ValueError('Unknown pilot phase')
    account=json.loads(os.environ['FCM_SERVICE_ACCOUNT_JSON'])
    token=target_token(account,json.loads(os.environ['PILOT_ENVELOPE']))
    print('::add-mask::'+token,flush=True)
    access=oauth(account)
    print('::add-mask::'+access,flush=True)
    count=1 if phase=='validate' else 3
    for index in range(count):
        probe='pilot-'+phase+'-'+str(index+1)+'-'+str(time.time_ns()//1_000_000)
        data=json.dumps(payload(token,phase,probe),ensure_ascii=True,separators=(',',':')).encode()
        request=urllib.request.Request('https://fcm.googleapis.com/v1/projects/'+PROJECT+'/messages:send',data=data,headers={'Authorization':'Bearer '+access,'Content-Type':'application/json'})
        started=time.time_ns()//1_000_000
        try:
            with urllib.request.urlopen(request,timeout=15) as response:
                status=response.status
                response.read()
        except urllib.error.HTTPError as error:
            result=json.loads(error.read())
            detail=result.get('error',{})
            # Do not print the provider body: it may echo the recipient token.
            print(json.dumps({'phase':phase,'status':error.code,'provider_status':detail.get('status'),'provider_message':str(detail.get('message','')).replace(token,'[redacted]')[:600],'request_bytes':len(data),'elapsed_ms':time.time_ns()//1_000_000-started}),flush=True)
            raise SystemExit(2)
        print(json.dumps({'phase':phase,'probe_id':probe,'status':status,'validate_only':phase=='validate','request_bytes':len(data),'send_started_ms':started,'ack_ms':time.time_ns()//1_000_000}),flush=True)
        if index+1<count:time.sleep(25)


if __name__ == '__main__':
    main()
