import logging
import re
import sre_constants

from django import forms
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.translation import ugettext as _

from reviewboard.diffviewer.forms import UploadDiffForm, EmptyDiffError
from reviewboard.reviews.errors import OwnershipError
from reviewboard.reviews.models import DefaultReviewer, ReviewRequest, \
                                       ReviewRequestDraft, Screenshot
from reviewboard.scmtools.errors import SCMError, ChangeNumberInUseError, \
                                        InvalidChangeNumberError, \
                                        ChangeSetError
from reviewboard.scmtools.models import Repository


class DefaultReviewerForm(forms.ModelForm):
    name = forms.CharField(
        label=_("Name"),
        max_length=64,
        widget=forms.TextInput(attrs={'size': '30'}))

    file_regex = forms.CharField(
        label=_("File regular expression"),
        max_length=256,
        widget=forms.TextInput(attrs={'size': '60'}),
        help_text=_('File paths are matched against this regular expression '
                    'to determine if these reviewers should be added.'))

    repository = forms.ModelMultipleChoiceField(
        label=_('Repositories'),
        required=False,
        queryset=Repository.objects.filter(visible=True).order_by('name'),
        help_text=_('The list of repositories to specifically match this '
                    'default reviewer for. If left empty, this will match '
                    'all repositories.'),
        widget=FilteredSelectMultiple(_("Repositories"), False))

    def clean_file_regex(self):
        """Validates that the specified regular expression is valid."""
        file_regex = self.cleaned_data['file_regex']

        try:
            re.compile(file_regex)
        except Exception, e:
            raise forms.ValidationError(e)

        return file_regex

    class Meta:
        model = DefaultReviewer


class NewReviewRequestFromBranchForm(forms.Form):
    """
    A form that handles creation of new review requests based on some
    branch.
    """
    repository = forms.ModelChoiceField(
        label=_("Repository"),
        queryset=Repository.objects.filter(visible=True).order_by('name'),
        empty_label=None,
        required=True)

    master_branch = forms.RegexField(
        regex=r'[\w_-]+(/[\w_-]+)?',
        label=_("Master branch"),
        required=True,
        widget=forms.TextInput(attrs={'size': '35'}))

    branch = forms.RegexField(
        regex=r'[\w_-]+(/[\w_-]+)?',
        label=_("Branch"),
        required=True,
        widget=forms.TextInput(attrs={'size': '35'}))

    branches = None

    def load_branches(self):
        repository = self.cleaned_data['repository']
        if not self.branches:
            scm_tool = repository.get_scmtool()
            scm_tool.update_cache()
            self.branches = scm_tool.get_branches()

    def branch_exists(self, branch):
        return 'origin/' + branch not in self.branches or 'remotes/origin/' + branch not in self.branches

    def clean_master_branch(self):
        self.load_branches()
        master_branch = self.cleaned_data['master_branch']

        if not self.branch_exists(master_branch):
            raise forms.ValidationError('Master branch does not exist')

        return master_branch

    def clean_branch(self):
        self.load_branches()
        branch = self.cleaned_data['branch']
        
        if not self.branch_exists(branch):
            raise forms.ValidationError('Branch does not exist')

        return branch

    def create(self, user):
        class SavedError(Exception):
            """Empty exception class for when we already saved the error info"""
            pass

        repository = self.cleaned_data['repository']
        master_branch = self.cleaned_data['master_branch']
        branch = self.cleaned_data['branch']

        scm_tool = repository.get_scmtool()

        diff_content = scm_tool.get_branches_diff('origin/' + master_branch, 'origin/' + branch)
        diff_file = SimpleUploadedFile("console", diff_content)

        diff_form = UploadDiffForm(repository,
        files={
            'path': diff_file,
        })
        diff_form.full_clean()

        review_request = ReviewRequest.objects.create(user, repository)
        review_request.master_branch = master_branch
        review_request.branch = branch

        try:
            diff_form.create(diff_file, None,
                             review_request.diffset_history)
            if 'path' in diff_form.errors:
                self.errors['branch'] = diff_form.errors['path']
                raise SavedError
            elif 'base_diff_path' in diff_form.errors:
                self.errors['branch'] = diff_form.errors['base_diff_path']
                raise SavedError
        except SavedError:
            review_request.delete()
            raise
        except EmptyDiffError:
            review_request.delete()
            self.errors['branch'] = \
                    forms.util.ErrorList(['Branch does not differ from trunk'])
            raise
        except Exception, e:
            review_request.delete()
            self.errors['branch'] = forms.util.ErrorList([e])
            raise

        review_request.add_default_reviewers()
        review_request.save()
        return review_request


class NewReviewRequestForm(forms.Form):
    """
    A form that handles creationg of new review requests. These take
    information on the diffs, the repository the diffs are against, and
    optionally a changelist number (for use in certain repository types
    such as Perforce).
    """
    basedir = forms.CharField(
        label=_("Base Directory"),
        required=False,
        help_text=_("The absolute path in the repository the diff was "
                    "generated in."),
        widget=forms.TextInput(attrs={'size': '35'}))
    diff_path = forms.FileField(
        label=_("Diff"),
        required=True,
        help_text=_("The new diff to upload."),
        widget=forms.FileInput(attrs={'size': '35'}))
    parent_diff_path = forms.FileField(
        label=_("Parent Diff"),
        required=False,
        help_text=_("An optional diff that the main diff is based on. "
                    "This is usually used for distributed revision control "
                    "systems (Git, Mercurial, etc.)."),
        widget=forms.FileInput(attrs={'size': '35'}))
    repository = forms.ModelChoiceField(
        label=_("Repository"),
        queryset=Repository.objects.filter(visible=True).order_by('name'),
        empty_label=None,
        required=True)

    changenum = forms.IntegerField(label=_("Change Number"), required=False)

    field_mapping = {}

    def __init__(self, *args, **kwargs):
        forms.Form.__init__(self, *args, **kwargs)

        # Repository ID : visible fields mapping.  This is so we can
        # dynamically show/hide the relevant fields with javascript.
        valid_repos = []
        repo_ids = [id for (id, name) in self.fields['repository'].choices]

        for repo in Repository.objects.filter(pk__in=repo_ids).order_by("name"):
            try:
                self.field_mapping[repo.id] = repo.get_scmtool().get_fields()
                valid_repos.append((repo.id, repo.name))
            except Exception, e:
                logging.error('Error loading SCMTool for repository '
                              '%s (ID %d): %s' % (repo.name, repo.id, e),
                              exc_info=1)

        self.fields['repository'].choices = valid_repos


    @staticmethod
    def create_from_list(data, constructor, error):
        """Helper function to combine the common bits of clean_target_people
           and clean_target_groups"""
        names = [x for x in map(str.strip, re.split(',\s*', data)) if x]
        return set([constructor(name) for name in names])

    def create(self, user, diff_file, parent_diff_file):
        repository = self.cleaned_data['repository']
        changenum = self.cleaned_data['changenum'] or None

        # It's a little odd to validate this here, but we want to have access to
        # the user.
        if changenum:
            try:
                changeset = repository.get_scmtool().get_changeset(changenum)
            except NotImplementedError:
                # This scmtool doesn't have changesets
                pass
            except SCMError, e:
                self.errors['changenum'] = forms.util.ErrorList([str(e)])
                raise ChangeSetError()
            except ChangeSetError, e:
                self.errors['changenum'] = forms.util.ErrorList([str(e)])
                raise e

            if not changeset:
                self.errors['changenum'] = forms.util.ErrorList([
                    'This change number does not represent a valid '
                    'changeset.'])
                raise InvalidChangeNumberError()

            if user.username != changeset.username:
                self.errors['changenum'] = forms.util.ErrorList([
                    'This change number is owned by another user.'])
                raise OwnershipError()

        try:
            review_request = ReviewRequest.objects.create(user, repository,
                                                          changenum)
        except ChangeNumberInUseError:
            # The user is updating an existing review request, rather than
            # creating a new one.
            review_request = ReviewRequest.objects.get(changenum=changenum)
            review_request.update_from_changenum(changenum)

            if review_request.status == 'D':
                # Act like we're creating a brand new review request if the
                # old one is discarded.
                review_request.status = 'P'
                review_request.public = False

            review_request.save()

        diff_form = UploadDiffForm(repository, data={
            'basedir': self.cleaned_data['basedir'],
        },
        files={
            'path': diff_file,
            'parent_diff_path': parent_diff_file,
        })
        diff_form.full_clean()

        class SavedError(Exception):
            """Empty exception class for when we already saved the error info"""
            pass

        try:
            diff_form.create(diff_file, parent_diff_file,
                             review_request.diffset_history)
            if 'path' in diff_form.errors:
                self.errors['diff_path'] = diff_form.errors['path']
                raise SavedError
            elif 'base_diff_path' in diff_form.errors:
                self.errors['base_diff_path'] = diff_form.errors['base_diff_path']
                raise SavedError
        except SavedError:
            review_request.delete()
            raise
        except EmptyDiffError:
            review_request.delete()
            self.errors['diff_path'] = forms.util.ErrorList([
                'The selected file does not appear to be a diff.'])
            raise
        except Exception, e:
            review_request.delete()
            self.errors['diff_path'] = forms.util.ErrorList([e])
            raise

        review_request.add_default_reviewers()
        review_request.save()
        return review_request


class UploadScreenshotForm(forms.Form):
    """
    A form that handles uploading of new screenshots.
    A screenshot takes a path argument and optionally a caption.
    """
    caption = forms.CharField(required=False)
    path = forms.ImageField(required=True)

    def create(self, file, review_request):
        screenshot = Screenshot(caption=self.cleaned_data['caption'],
                                draft_caption=self.cleaned_data['caption'])
        screenshot.image.save(file.name, file, save=True)

        review_request.screenshots.add(screenshot)

        draft = ReviewRequestDraft.create(review_request)
        draft.screenshots.add(screenshot)
        draft.save()

        return screenshot
