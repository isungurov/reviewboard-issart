import re
import xmlrpclib

jira_server = 'http://issues.apdbox.com/rpc/xmlrpc'
jira_username = 'svn'
jira_password = 'svn!!'

def get_ticket(ticket_key):
    s = xmlrpclib.ServerProxy(jira_server)
    auth = s.jira1.login(jira_username, jira_password)
    issue = s.jira1.getIssue(auth, ticket_key)
    s.jira1.logout(auth)
    return issue

def build_review_request(review_request):
    ticket_key = review_request.branch[len('origin/'):]

    try:
        issue = get_ticket(ticket_key)
        review_request.summary = issue['summary']
        review_request.bugs_closed = ticket_key
    except:
        review_request.summary = ticket_key

    review_request.save()

class ReviewRequestBuildException(Exception):
    pass