import re
import xmlrpclib

jira_server = 'http://issues.apdbox.com/rpc/xmlrpc'
jira_username = 'svn'
jira_password = 'svn!!'

#log_message_re = re.compile(
            #r'\[(?P<ticket>[A-Z]+-(?P<ticket_number>\d+))\]\s*(?P<message>.*)$',
            #re.DOTALL)

def get_ticket(ticket_key):
    s = xmlrpclib.ServerProxy(jira_server)
    auth = s.jira1.login(jira_username, jira_password)
    issue = s.jira1.getIssue(auth, ticket_key)
    s.jira1.logout(auth)
    return issue

def build_review_request(review_request):
    #scm_tool = review_request.repository.get_scmtool()

    #logs = scm_tool.get_branch_log('origin/' + review_request.branch, limit=1)
    #if len(logs) == 0:
        #return
    #log_entry = logs[0]

    #m = log_message_re.search(log_entry.message)
    #if not m:
        #raise ReviewRequestBuildException(
            #'Log message does not match required pattern: ' + log_entry.message)

    #ticket_key = m.group('ticket')
    ticket_key = review_request.branch[len('origin/'):]

    try:
        issue = get_ticket(ticket_key)
        review_request.summary = issue['summary']
        review_request.description = issue.get('description','')
        review_request.bugs_closed = ticket_key
    except:
        review_request.summary = ticket_key

    review_request.save()

class ReviewRequestBuildException(Exception):
    pass