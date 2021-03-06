"""
Workflow models are intended to track which step the student is in during the
assessment process. The submission state is not explicitly tracked because
the assessment workflow only begins after a submission has been created.

NOTE: We've switched to migrations, so if you make any edits to this file, you
need to then generate a matching migration for it using:

    ./manage.py schemamigration openassessment.workflow --auto

"""
import logging
import importlib
from django.conf import settings
from django.db import models, transaction, DatabaseError
from django.dispatch import receiver
from django_extensions.db.fields import UUIDField
from django.utils.timezone import now
from model_utils import Choices
from model_utils.models import StatusModel, TimeStampedModel
from submissions import api as sub_api
from openassessment.assessment.signals import assessment_complete_signal
from .errors import AssessmentApiLoadError


logger = logging.getLogger('openassessment.workflow.models')


# To encapsulate the workflow API from the assessment API,
# we use dependency injection.  The Django settings define
# a dictionary mapping assessment step names to the Python module path
# that implements the corresponding assessment API.
# For backwards compatibility, we provide a default configuration as well
DEFAULT_ASSESSMENT_API_DICT = {
    'peer': 'openassessment.assessment.api.peer',
    'self': 'openassessment.assessment.api.self',
    'training': 'openassessment.assessment.api.student_training',
    'ai': 'openassessment.assessment.api.ai',
}
ASSESSMENT_API_DICT = getattr(
    settings, 'ORA2_ASSESSMENTS',
    DEFAULT_ASSESSMENT_API_DICT
)

# For now, we use a simple scoring mechanism:
# Once a student has completed all assessments,
# we search assessment APIs
# in priority order until one of the APIs provides a score.
# We then use that score as the student's overall score.
# This Django setting is a list of assessment steps (defined in `settings.ORA2_ASSESSMENTS`)
# in descending priority order.
DEFAULT_ASSESSMENT_SCORE_PRIORITY = ['peer', 'self', 'ai']
ASSESSMENT_SCORE_PRIORITY = getattr(
    settings, 'ORA2_ASSESSMENT_SCORE_PRIORITY',
    DEFAULT_ASSESSMENT_SCORE_PRIORITY
)


class AssessmentWorkflow(TimeStampedModel, StatusModel):
    """Tracks the open-ended assessment status of a student submission.

    It's important to note that although we track the status as an explicit
    field here, it is not the canonical status. This is because the
    determination of what we need to do in order to be "done" is specified by
    the OpenAssessmentBlock problem definition and can change. So every time
    we are asked where the student is, we have to query the peer, self, and
    later other assessment APIs with the latest requirements (e.g. "number of
    submissions you have to assess = 5"). The "status" field on this model is
    an after the fact recording of the last known state of that information so
    we can search easily.
    """
    STEPS = ASSESSMENT_API_DICT.keys()

    STATUSES = [
        "waiting",  # User has done all necessary assessment but hasn't been
                    # graded yet -- we're waiting for assessments of their
                    # submission by others.
        "done",  # Complete
    ]

    STATUS_VALUES = STEPS + STATUSES

    STATUS = Choices(*STATUS_VALUES)  # implicit "status" field

    submission_uuid = models.CharField(max_length=36, db_index=True, unique=True)
    uuid = UUIDField(version=1, db_index=True, unique=True)

    # These values are used to find workflows for a particular item
    # in a course without needing to look up the submissions for that item.
    # Because submissions are immutable, we can safely duplicate the values
    # here without violating data integrity.
    course_id = models.CharField(max_length=255, blank=False, db_index=True)
    item_id = models.CharField(max_length=255, blank=False, db_index=True)

    class Meta:
        ordering = ["-created"]
        # TODO: In migration, need a non-unique index on (course_id, item_id, status)

    @classmethod
    @transaction.commit_on_success
    def start_workflow(cls, submission_uuid, step_names, on_init_params):
        """
        Start a new workflow.

        Args:
            submission_uuid (str): The UUID of the submission associated with this workflow.
            step_names (list): The names of the assessment steps in the workflow.
            on_init_params (dict): The parameters to pass to each assessment module
                on init.  Keys are the assessment step names.

        Returns:
            AssessmentWorkflow

        Raises:
            SubmissionNotFoundError
            SubmissionRequestError
            SubmissionInternalError
            DatabaseError
            Assessment-module specific errors
        """
        submission_dict = sub_api.get_submission_and_student(submission_uuid)

        # Create the workflow and step models in the database
        # For now, set the status to waiting; we'll modify it later
        # based on the first step in the workflow.
        workflow = cls.objects.create(
            submission_uuid=submission_uuid,
            status=AssessmentWorkflow.STATUS.waiting,
            course_id=submission_dict['student_item']['course_id'],
            item_id=submission_dict['student_item']['item_id']
        )
        workflow_steps = [
            AssessmentWorkflowStep(
                workflow=workflow, name=step, order_num=i
            )
            for i, step in enumerate(step_names)
        ]
        workflow.steps.add(*workflow_steps)

        # Initialize the assessment APIs
        has_started_first_step = False
        for step in workflow_steps:
            api = step.api()

            if api is not None:
                # Initialize the assessment module
                # We do this for every assessment module
                on_init_func = getattr(api, 'on_init', lambda submission_uuid, **params: None)
                on_init_func(submission_uuid, **on_init_params.get(step.name, {}))

                # For the first valid step, update the workflow status
                # and notify the assessment module that it's being started
                if not has_started_first_step:
                    # Update the workflow
                    workflow.status = step.name
                    workflow.save()

                    # Notify the assessment module that it's being started
                    on_start_func = getattr(api, 'on_start', lambda submission_uuid: None)
                    on_start_func(submission_uuid)

                    # Remember that we've already started the first step
                    has_started_first_step = True

        # Update the workflow (in case some of the assessment modules are automatically complete)
        # We do NOT pass in requirements, on the assumption that any assessment module
        # that accepts requirements would NOT automatically complete.
        workflow.update_from_assessments(None)

        # Return the newly created workflow
        return workflow

    @property
    def score(self):
        """Latest score for the submission we're tracking.

        Note that while it is usually the case that we're setting the score,
        that may not always be the case. We may have some course staff override.
        """
        return sub_api.get_latest_score_for_submission(self.submission_uuid)

    def status_details(self, assessment_requirements):
        status_dict = {}
        steps = self._get_steps()
        for step in steps:
            api = step.api()
            if api is not None:
                # If an assessment module does not define these functions,
                # default to True -- that is, automatically assume that the user has
                # met the requirements.  This prevents students from getting "stuck"
                # in the workflow in the event of a rollback that removes a step
                # from the problem definition.
                submitter_finished_func = getattr(api, 'submitter_is_finished', lambda submission_uuid, reqs: True)
                assessment_finished_func = getattr(api, 'assessment_is_finished', lambda submission_uuid, reqs: True)

                status_dict[step.name] = {
                    "complete": submitter_finished_func(
                        self.submission_uuid,
                        assessment_requirements.get(step.name, {})
                    ),
                    "graded": assessment_finished_func(
                        self.submission_uuid,
                        assessment_requirements.get(step.name, {})
                    ),
                }
        return status_dict

    def update_from_assessments(self, assessment_requirements):
        """Query assessment APIs and change our status if appropriate.

        If the status is done, we do nothing. Once something is done, we never
        move back to any other status.

        If an assessment API says that our submitter's requirements are met,
        then move to the next assessment.  For example, in peer assessment,
        if the submitter we're tracking has assessed the required number
        of submissions, they're allowed to continue.

        If the submitter has finished all the assessments, then we change
        their status to `waiting`.

        If we're in the `waiting` status, and an assessment API says it can score
        this submission, then we record the score in the submissions API and move our
        `status` to `done`.

        By convention, if `assessment_requirements` is `None`, then assessment
        modules that need requirements should automatically say that they're incomplete.
        This allows us to update the workflow even when we don't know the
        current state of the problem.  For example, if we're updating the workflow
        at the completion of an asynchronous call, we won't necessarily know the
        current state of the problem, but we would still want to update assessments
        that don't have any requirements.

        Args:
            assessment_requirements (dict): Dictionary passed to the assessment API.
                This defines the requirements for each assessment step; the APIs
                can refer to this to decide whether the requirements have been
                met.  Note that the requirements could change if the author
                updates the problem definition.

        """
        # If we're done, we're done -- it doesn't matter if requirements have
        # changed because we've already written a score.
        if self.status == self.STATUS.done:
            return

        # Update our AssessmentWorkflowStep models with the latest from our APIs
        steps = self._get_steps()
        step_for_name = {step.name:step for step in steps}

        # Go through each step and update its status.
        for step in steps:
            step.update(self.submission_uuid, assessment_requirements)

        # Fetch name of the first step that the submitter hasn't yet completed.
        new_status = next(
            (step.name for step in steps if step.submitter_completed_at is None),
            self.STATUS.waiting  # if nothing's left to complete, we're waiting
        )

        # If the submitter is beginning the next assessment, notify the
        # appropriate assessment API.
        new_step = step_for_name.get(new_status)
        if new_step is not None:
            on_start_func = getattr(new_step.api(), 'on_start', None)
            if on_start_func is not None:
                on_start_func(self.submission_uuid)

        # If the submitter has done all they need to do, let's check to see if
        # all steps have been fully assessed (i.e. we can score it).
        if (new_status == self.STATUS.waiting and
            all(step.assessment_completed_at for step in steps)):

            # At this point, we're trying to give a score. We currently have a
            # very simple rule for this -- we iterate through the
            # assessment APIs in priority order and use the first reported score.
            score = None
            for assessment_step_name in ASSESSMENT_SCORE_PRIORITY:

                # Check if the problem contains this assessment type
                assessment_step = step_for_name.get(assessment_step_name)

                # Query the corresponding assessment API for a score
                # If we find one, then stop looking
                if assessment_step is not None:

                    # Check if the assessment API defines a score function at all
                    get_score_func = getattr(assessment_step.api(), 'get_score', None)
                    if get_score_func is not None:
                        if assessment_requirements is None:
                            requirements = None
                        else:
                            requirements = assessment_requirements.get(assessment_step_name, {})
                        score = get_score_func(self.submission_uuid, requirements)
                        break

            # If we found a score, then we're done
            if score is not None:
                self.set_score(score)
                new_status = self.STATUS.done

        # Finally save our changes if the status has changed
        if self.status != new_status:
            self.status = new_status
            self.save()
            logger.info((
                u"Workflow for submission UUID {uuid} has updated status to {status}"
            ).format(uuid=self.submission_uuid, status=new_status))

    def _get_steps(self):
        """
        Simple helper function for retrieving all the steps in the given
        Workflow.
        """
        # Do not return steps that are not recognized in the AssessmentWorkflow.
        steps = list(self.steps.filter(name__in=AssessmentWorkflow.STEPS))
        if not steps:
            # If no steps exist for this AssessmentWorkflow, assume
            # peer -> self for backwards compatibility
            self.steps.add(
                AssessmentWorkflowStep(name=self.STATUS.peer, order_num=0),
                AssessmentWorkflowStep(name=self.STATUS.self, order_num=1)
            )
            steps = list(self.steps.all())
        return steps

    def set_score(self, score):
        """
        Set a score for the workflow.

        Scores are persisted via the Submissions API, separate from the Workflow
        Data. Score is associated with the same submission_uuid as this workflow

        Args:
            score (dict): A dict containing 'points_earned' and
                'points_possible'.

        """
        sub_api.set_score(
            self.submission_uuid,
            score["points_earned"],
            score["points_possible"]
        )


class AssessmentWorkflowStep(models.Model):
    """An individual step in the overall workflow process.

    Similar caveats apply to this class as apply to `AssessmentWorkflow`. What
    we're storing in the database is usually but not always current information.
    In particular, if the problem definition has changed the requirements for a
    particular step in the workflow, then what is in the database will be out of
    sync until someone views this problem again (which will trigger a workflow
    update to occur).

    """
    workflow = models.ForeignKey(AssessmentWorkflow, related_name="steps")
    name = models.CharField(max_length=20)
    submitter_completed_at = models.DateTimeField(default=None, null=True)
    assessment_completed_at = models.DateTimeField(default=None, null=True)
    order_num = models.PositiveIntegerField()

    class Meta:
        ordering = ["workflow", "order_num"]

    def is_submitter_complete(self):
        return self.submitter_completed_at is not None

    def is_assessment_complete(self):
        return self.assessment_completed_at is not None

    def api(self):
        """
        Returns an API associated with this workflow step. If no API is
        associated with this workflow step, None is returned.

        This relies on Django settings to map step names to
        the assessment API implementation.
        """
        # We retrieve the settings in-line here (rather than using the
        # top-level constant), so that @override_settings will work
        # in the test suite.
        api_path = getattr(
            settings, 'ORA2_ASSESSMENTS', DEFAULT_ASSESSMENT_API_DICT
        ).get(self.name)
        if api_path is not None:
            try:
                return importlib.import_module(api_path)
            except (ImportError, ValueError):
                raise AssessmentApiLoadError(self.name, api_path)
        else:
            # It's possible for the database to contain steps for APIs
            # that are not configured -- for example, if a new assessment
            # type is added, then the code is rolled back.
            msg = (
                u"No assessment configured for '{name}'.  "
                u"Check the ORA2_ASSESSMENTS Django setting."
            ).format(name=self.name)
            logger.warning(msg)
            return None

    def update(self, submission_uuid, assessment_requirements):
        """
        Updates the AssessmentWorkflowStep models with the requirements
        specified from the Workflow API.

        Intended for internal use by update_from_assessments(). See
        update_from_assessments() documentation for more details.
        """
        # Once a step is completed, it will not be revisited based on updated requirements.
        step_changed = False
        if assessment_requirements is None:
            step_reqs = None
        else:
            step_reqs = assessment_requirements.get(self.name, {})

        default_finished = lambda submission_uuid, step_reqs: True
        submitter_finished = getattr(self.api(), 'submitter_is_finished', default_finished)
        assessment_finished = getattr(self.api(), 'assessment_is_finished', default_finished)

        # Has the user completed their obligations for this step?
        if (not self.is_submitter_complete() and submitter_finished(submission_uuid, step_reqs)):
            self.submitter_completed_at = now()
            step_changed = True

        # Has the step received a score?
        if (not self.is_assessment_complete() and assessment_finished(submission_uuid, step_reqs)):
            self.assessment_completed_at = now()
            step_changed = True

        if step_changed:
            self.save()


@receiver(assessment_complete_signal)
def update_workflow_async(sender, **kwargs):
    """
    Register a receiver for the update workflow signal
    This allows asynchronous processes to update the workflow

    Args:
        sender (object): Not used

    Keyword Arguments:
        submission_uuid (str): The UUID of the submission associated
            with the workflow being updated.

    Returns:
        None

    """
    submission_uuid = kwargs.get('submission_uuid')
    if submission_uuid is None:
        logger.error("Update workflow signal called without a submission UUID")
        return

    try:
        workflow = AssessmentWorkflow.objects.get(submission_uuid=submission_uuid)
        workflow.update_from_assessments(None)
    except AssessmentWorkflow.DoesNotExist:
        msg = u"Could not retrieve workflow for submission with UUID {}".format(submission_uuid)
        logger.exception(msg)
    except DatabaseError:
        msg = (
            u"Database error occurred while updating "
            u"the workflow for submission UUID {}"
        ).format(submission_uuid)
        logger.exception(msg)
    except:
        msg = (
            u"Unexpected error occurred while updating the workflow "
            u"for submission UUID {}"
        ).format(submission_uuid)
        logger.exception(msg)
