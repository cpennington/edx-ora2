# -*- coding: utf-8 -*-
"""
Tests for self-assessment API.
"""

import copy
import datetime
import pytz
from django.test import TestCase
from submissions.api import create_submission
from openassessment.assessment.self_api import (
    create_assessment, get_submission_and_assessment,
    SelfAssessmentRequestError
)


class TestSelfApi(TestCase):

    STUDENT_ITEM = {
        'student_id': u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
        'item_id': 'test_item',
        'course_id': 'test_course',
        'item_type': 'test_type'
    }

    RUBRIC = {
        "criteria": [
            {
                "name": "clarity",
                "prompt": "How clear was it?",
                "options": [
                    {"name": "somewhat clear", "points": 1, "explanation": ""},
                    {"name": "clear", "points": 3, "explanation": ""},
                    {"name": "very clear", "points": 5, "explanation": ""},
                ]
            },
            {
                "name": "accuracy",
                "prompt": "How accurate was the content?",
                "options": [
                    {"name": "somewhat accurate", "points": 1, "explanation": ""},
                    {"name": "accurate", "points": 3, "explanation": ""},
                    {"name": "very accurate", "points": 5, "explanation": ""},
                ]
            },
        ]
    }

    OPTIONS_SELECTED = {
        "clarity": "clear",
        "accuracy": "very accurate",
    }

    def test_create_assessment(self):
        # Initially, there should be no submission or self assessment
        self.assertEqual(get_submission_and_assessment(self.STUDENT_ITEM), (None, None))

        # Create a submission to self-assess
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Now there should be a submission, but no self-assessment
        received_submission, assessment = get_submission_and_assessment(self.STUDENT_ITEM)
        self.assertItemsEqual(received_submission, submission)
        self.assertIs(assessment, None)

        # Create a self-assessment for the submission
        assessment = create_assessment(
            submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
            self.OPTIONS_SELECTED, self.RUBRIC,
            scored_at=datetime.datetime(2014, 4, 1).replace(tzinfo=pytz.utc)
        )

        # Retrieve the self-assessment
        received_submission, retrieved = get_submission_and_assessment(self.STUDENT_ITEM)
        self.assertItemsEqual(received_submission, submission)

        # Check that the assessment we created matches the assessment we retrieved
        # and that both have the correct values
        self.assertItemsEqual(assessment, retrieved)
        self.assertEqual(assessment['submission_uuid'], submission['uuid'])
        self.assertEqual(assessment['points_earned'], 8)
        self.assertEqual(assessment['points_possible'], 10)
        self.assertEqual(assessment['feedback'], u'')
        self.assertEqual(assessment['score_type'], u'SE')

    def test_create_assessment_no_submission(self):
        # Attempt to create a self-assessment for a submission that doesn't exist
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                'invalid_submission_uuid', u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
                self.OPTIONS_SELECTED, self.RUBRIC,
                scored_at=datetime.datetime(2014, 4, 1)
            )

    def test_create_assessment_wrong_user(self):
        # Create a submission
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Attempt to create a self-assessment for the submission from a different user
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                'invalid_submission_uuid', u'another user',
                self.OPTIONS_SELECTED, self.RUBRIC,
                scored_at=datetime.datetime(2014, 4, 1)
            )

    def test_create_assessment_invalid_criterion(self):
        # Create a submission
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Mutate the selected option criterion so it does not match a criterion in the rubric
        options = copy.deepcopy(self.OPTIONS_SELECTED)
        options['invalid criterion'] = 'very clear'

        # Attempt to create a self-assessment with options that do not match the rubric
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
                options, self.RUBRIC,
                scored_at=datetime.datetime(2014, 4, 1)
            )

    def test_create_assessment_invalid_option(self):
        # Create a submission
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Mutate the selected option so the value does not match an available option
        options = copy.deepcopy(self.OPTIONS_SELECTED)
        options['clarity'] = 'invalid option'

        # Attempt to create a self-assessment with options that do not match the rubric
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
                options, self.RUBRIC,
                scored_at=datetime.datetime(2014, 4, 1)
            )

    def test_create_assessment_missing_critieron(self):
        # Create a submission
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Delete one of the criterion that's present in the rubric
        options = copy.deepcopy(self.OPTIONS_SELECTED)
        del options['clarity']

        # Attempt to create a self-assessment with options that do not match the rubric
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
                options, self.RUBRIC,
                scored_at=datetime.datetime(2014, 4, 1)
            )

    def test_create_assessment_timestamp(self):
        # Create a submission to self-assess
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Record the current system clock time
        before = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

        # Create a self-assessment for the submission
        # Do not override the scored_at timestamp, so it should be set to the current time
        assessment = create_assessment(
            submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
            self.OPTIONS_SELECTED, self.RUBRIC,
        )

        # Retrieve the self-assessment
        _, retrieved = get_submission_and_assessment(self.STUDENT_ITEM)

        # Expect that both the created and retrieved assessments have the same
        # timestamp, and it's >= our recorded time.
        self.assertEqual(assessment['scored_at'], retrieved['scored_at'])
        self.assertGreaterEqual(assessment['scored_at'], before)

    def test_create_multiple_self_assessments(self):
        # Create a submission to self-assess
        submission = create_submission(self.STUDENT_ITEM, "Test answer")

        # Self assess once
        assessment = create_assessment(
            submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
            self.OPTIONS_SELECTED, self.RUBRIC,
        )

        # Attempt to self-assess again, which should raise an exception
        with self.assertRaises(SelfAssessmentRequestError):
            create_assessment(
                submission['uuid'], u'𝖙𝖊𝖘𝖙 𝖚𝖘𝖊𝖗',
                self.OPTIONS_SELECTED, self.RUBRIC,
            )

        # Expect that we still have the original assessment
        _, retrieved = get_submission_and_assessment(self.STUDENT_ITEM)
        self.assertItemsEqual(assessment, retrieved)