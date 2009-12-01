from django_evolution.mutations import AddField
from django.db import models


MUTATIONS = [
    AddField('ReviewRequest', 'master_branch', models.CharField, max_length=300, blank=True, initial='')
]
