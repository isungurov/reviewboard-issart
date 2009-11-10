import re
import xmlrpclib

from django.db.models import Q
from reviewboard.reviews.models import Group

jira_server = 'http://issues.apdbox.com/rpc/xmlrpc'
jira_username = 'isungurov'
jira_password = '<shira>'

log_message_re = re.compile(
            r'\[(?P<ticket>[A-Z]+-(?P<ticket_number>\d+))\]\s*(?P<message>.*)$',
            re.DOTALL)

groups_associations = [{
        'base-path': r'^/tcc2/trunk',
        'default-group': 'tcc2-other',
        'paths': {
            r'/tcc/web/(([^p][^h][^p][^/].*)|(.*\.[^p][^h][^p]$))': 'tcc2-js',
            r'/tcc/web/((php/.*)|(.*\.php$))': 'tcc2-php',
            r'/((tcc/src)|(tomcat))/': 'tcc2-java',
        }
    }, {
        'base-path': r'^/visualorganizer/trunk',
        'default-group': 'vo',
        'paths': {
        }
    }
]

default_group = 'other'

def get_file_groups(filename):
    groups = set()

    for group_association in groups_associations:
        if not re.match(group_association['base-path'], filename):
            continue

        if len(group_association['paths']) != 0:
            add_default = True

            for (path_re, group) in group_association['paths'].items():
                full_path_re = group_association['base-path'] + path_re
                if re.match(full_path_re, filename):
                    groups.add(group)
                    add_default = False

            if add_default:
                groups.add(group_association['default-group'])
        else:
            groups.add(group_association['default-group'])

    if len(groups) == 0:
        groups.add(default_group)

    return groups

def get_ticket(ticket_key):
    s = xmlrpclib.ServerProxy(jira_server)
    auth = s.jira1.login(jira_username, jira_password)
    issue = s.jira1.getIssue(auth, ticket_key)
    s.jira1.logout(auth)
    return issue

def build_review_request(review_request):
    scm_tool = review_request.repository.get_scmtool()

    logs = scm_tool.get_branch_log(review_request.branch, limit=1)
    if len(logs) == 0:
        return
    log_entry = logs[0]

    m = log_message_re.search(log_entry.message)
    if not m:
        raise ReviewRequestBuildException("Invalid log message: " + log_entry)

    ticket_key = m.group('ticket')
    issue = get_ticket(ticket_key)
    review_request.bugs_closed = ticket_key
    review_request.summary = issue['summary']
    review_request.description = issue.get('description','')

    groups = set()

    filenames = scm_tool.get_filenames_in_branch(review_request.branch)
    for filename in filenames:
        file_groups = get_file_groups(filename)
        groups.update(file_groups)
    
    for group_name in groups:
        group = Group.objects.get(Q(name__iexact=group_name))
        review_request.target_groups.add(group)

    review_request.save()

class ReviewRequestBuildException(Exception):
    pass