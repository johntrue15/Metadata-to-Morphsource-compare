"""
Tests for the issue comment reply workflow.
Ensures that follow-up questions on query issues trigger the conversation continuation flow.
"""

import os
import yaml
import pytest


class TestIssueCommentReplyWorkflow:
    """Test the issue-comment-reply workflow configuration and logic."""
    
    @pytest.fixture
    def workflow_file(self):
        """Load the workflow file."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        with open(workflow_path, 'r') as f:
            return yaml.safe_load(f)
    
    def test_workflow_exists(self):
        """Test that the issue-comment-reply workflow file exists."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        assert os.path.exists(workflow_path), "Workflow file should exist"
    
    def test_workflow_has_correct_trigger(self, workflow_file):
        """Test that workflow triggers on issue_comment created events."""
        assert 'on' in workflow_file
        assert 'issue_comment' in workflow_file['on']
        assert 'types' in workflow_file['on']['issue_comment']
        assert 'created' in workflow_file['on']['issue_comment']['types']
    
    def test_workflow_has_required_permissions(self, workflow_file):
        """Test that workflow has necessary permissions."""
        assert 'jobs' in workflow_file
        assert 'handle-reply' in workflow_file['jobs']
        
        job = workflow_file['jobs']['handle-reply']
        assert 'permissions' in job
        
        permissions = job['permissions']
        assert permissions['issues'] == 'write'
        assert permissions['actions'] == 'write'
        assert permissions['contents'] == 'read'
    
    def test_workflow_has_check_step(self, workflow_file):
        """Test that workflow includes a step to check if processing is needed."""
        job = workflow_file['jobs']['handle-reply']
        steps = job['steps']
        
        # Find the check step
        check_step = None
        for step in steps:
            if step.get('id') == 'check':
                check_step = step
                break
        
        assert check_step is not None, "Should have a 'check' step"
        assert check_step['name'] == 'Check if this is a conversation continuation'
        assert 'uses' in check_step
        assert 'actions/github-script@v8' in check_step['uses']
    
    def test_workflow_has_extract_context_step(self, workflow_file):
        """Test that workflow extracts conversation context."""
        job = workflow_file['jobs']['handle-reply']
        steps = job['steps']
        
        # Find the extract context step
        extract_step = None
        for step in steps:
            if step.get('id') == 'extract_context':
                extract_step = step
                break
        
        assert extract_step is not None, "Should have an 'extract_context' step"
        assert extract_step['name'] == 'Extract conversation context'
        assert extract_step['if'] == "steps.check.outputs.should_process == 'true'"
    
    def test_workflow_adds_acknowledgment(self, workflow_file):
        """Test that workflow adds an acknowledgment comment."""
        job = workflow_file['jobs']['handle-reply']
        steps = job['steps']
        
        # Find acknowledgment step
        ack_step = None
        for step in steps:
            if 'acknowledgment' in step.get('name', '').lower():
                ack_step = step
                break
        
        assert ack_step is not None, "Should have an acknowledgment step"
        assert ack_step['if'] == "steps.check.outputs.should_process == 'true'"
    
    def test_workflow_triggers_query_processor(self, workflow_file):
        """Test that workflow triggers the query processor."""
        job = workflow_file['jobs']['handle-reply']
        steps = job['steps']
        
        # Find the trigger step
        trigger_step = None
        for step in steps:
            if 'trigger' in step.get('name', '').lower() and 'query processor' in step.get('name', '').lower():
                trigger_step = step
                break
        
        assert trigger_step is not None, "Should have a step to trigger query processor"
        assert trigger_step['if'] == "steps.check.outputs.should_process == 'true'"
        
        # Check that it uses environment variables
        assert 'env' in trigger_step
        env_vars = trigger_step['env']
        assert 'FOLLOW_UP_QUESTION' in env_vars
        assert 'CONVERSATION_CONTEXT' in env_vars


class TestConversationContextLogic:
    """Test the conversation context building logic."""
    
    def test_context_includes_original_query(self):
        """Test that conversation context should include original query."""
        # This is a conceptual test - the actual logic is in the workflow YAML
        # We verify that the workflow script includes logic for extracting original query
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        
        with open(workflow_path, 'r') as f:
            content = f.read()
        
        # Check that the workflow extracts original query
        assert 'original_query' in content.lower()
        assert 'Original Question:' in content or 'Original query:' in content
    
    def test_context_includes_previous_response(self):
        """Test that conversation context should include previous bot response."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        
        with open(workflow_path, 'r') as f:
            content = f.read()
        
        # Check that the workflow extracts previous responses
        assert 'Previous Response:' in content or 'lastBotResponse' in content
        assert 'ChatGPT Response' in content
    
    def test_context_includes_follow_up(self):
        """Test that conversation context includes the follow-up question."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        
        with open(workflow_path, 'r') as f:
            content = f.read()
        
        # Check that follow-up question is included
        assert 'Follow-up Question:' in content or 'follow_up_question' in content


class TestBotDetection:
    """Test that the workflow correctly identifies and skips bot comments."""
    
    def test_bot_detection_logic_exists(self):
        """Test that workflow has logic to detect bot comments."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        
        with open(workflow_path, 'r') as f:
            content = f.read()
        
        # Check for bot detection logic
        assert 'isBot' in content or 'is_bot' in content
        assert "type === 'Bot'" in content or 'Bot' in content
        assert 'skipping' in content.lower()


class TestQueryIssueDetection:
    """Test that workflow correctly identifies query issues."""
    
    def test_query_issue_label_check(self):
        """Test that workflow checks for query-request label."""
        workflow_path = os.path.join(
            os.path.dirname(__file__),
            '..',
            '.github',
            'workflows',
            'issue-comment-reply.yml'
        )
        
        with open(workflow_path, 'r') as f:
            content = f.read()
        
        # Check for query issue detection
        assert 'query-request' in content
        assert 'labels' in content.lower()
