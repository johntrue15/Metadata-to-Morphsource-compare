"""
Test response grader workflow and script functionality
"""
import os
import json
import pytest
import sys

yaml = pytest.importorskip("yaml")

# Add the scripts directory to the path for importing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '.github', 'scripts'))


class TestResponseGraderWorkflow:
    """Test the response grader workflow structure"""
    
    def test_response_grader_workflow_syntax(self):
        """Test that response-grader.yml has valid YAML syntax"""
        workflow_path = '.github/workflows/response-grader.yml'
        assert os.path.exists(workflow_path), "response-grader.yml not found"
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        assert workflow is not None, "Failed to parse workflow YAML"
        assert 'jobs' in workflow, "Workflow must have jobs"
    
    def test_response_grader_has_required_steps(self):
        """Test that response-grader.yml has all required steps"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        step_names = [step.get('name', '') for step in job['steps']]
        
        required_steps = [
            'Check if this is a query response',
            'Extract query and response',
            'Grade all responses',
            'Post grades to issue'
        ]
        
        for required_step in required_steps:
            assert required_step in step_names, f"Step '{required_step}' must exist"
    
    def test_extract_step_handles_multiple_responses(self):
        """Test that extract step collects all responses, not just the first"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        extract_step = None
        for step in job['steps']:
            if step.get('name') == 'Extract query and response':
                extract_step = step
                break
        
        assert extract_step is not None, "Extract step must exist"
        
        # Check that the script collects all responses
        script = extract_step['with']['script']
        
        # Should not have 'break' after finding first response
        assert 'const responses = []' in script, "Should collect multiple responses"
        assert 'responses.push({' in script, "Should push responses to array"
        
        # Should filter already graded responses
        assert 'gradedCommentIds' in script, "Should track graded comment IDs"
        assert 'ungradedResponses' in script, "Should filter to ungraded responses"
        
    def test_grade_step_processes_all_responses(self):
        """Test that grade step processes all responses"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        grade_step = None
        for step in job['steps']:
            if step.get('name') == 'Grade all responses':
                grade_step = step
                break
        
        assert grade_step is not None, "Grade step must exist"
        
        # Check that the script processes all responses
        run_script = grade_step['run']
        
        assert 'responses.json' in run_script, "Should read responses from file"
        assert 'for i, response_data in enumerate(responses)' in run_script, \
            "Should iterate through all responses"
        assert 'all_grades' in run_script, "Should collect all grades"
        assert 'all_grades.json' in run_script, "Should write all grades to file"
    
    def test_post_step_posts_multiple_grades(self):
        """Test that post step posts a grade for each response"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        post_step = None
        for step in job['steps']:
            if step.get('name') == 'Post grades to issue':
                post_step = step
                break
        
        assert post_step is not None, "Post step must exist"
        
        # Check that the script posts multiple grades
        script = post_step['with']['script']
        
        assert 'all_grades.json' in script, "Should read all grades from file"
        assert 'for (let i = 0; i < allGrades.length; i++)' in script, \
            "Should iterate through all grades"
        
        # Should include response numbering
        assert 'Response ${i+1}/${allGrades.length}' in script, \
            "Should number responses when multiple exist"
        
        # Should include graded comment ID marker
        assert 'graded-comment-id:' in script, \
            "Should mark which response was graded"
    
    def test_workflow_allows_grading_new_responses(self):
        """Test that workflow can grade new responses even after initial grading"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        check_step = None
        for step in job['steps']:
            if step.get('name') == 'Check if this is a query response':
                check_step = step
                break
        
        assert check_step is not None, "Check step must exist"
        
        # The script should NOT prevent grading if 'graded' label exists
        # Instead, it relies on the extract step to filter ungraded responses
        script = check_step['with']['script']
        
        # Should not have a blocking check for 'graded' label
        assert "if (isGraded)" not in script or \
               "Issue already graded, skipping" not in script, \
            "Should not block grading if 'graded' label exists"


class TestResponseGraderScript:
    """Test the grade_response.py script functionality"""
    
    def test_grade_response_function_exists(self):
        """Test that grade_response function can be imported"""
        try:
            from grade_response import grade_response
            assert callable(grade_response), "grade_response should be callable"
        except ImportError as e:
            pytest.skip(f"Cannot import grade_response: {e}")
    
    def test_grade_response_returns_dict(self):
        """Test that grade_response returns a properly formatted dict"""
        try:
            from grade_response import grade_response
        except ImportError:
            pytest.skip("Cannot import grade_response")
        
        # Mock test data
        query = "Show me alligator skulls"
        response_text = "I found 36 Alligator specimens."
        morphosource_results = {"count": 36, "status": "success"}
        
        # Note: This test requires OPENAI_API_KEY to be set
        # If not available, the function should return an error dict
        result = grade_response(query, response_text, morphosource_results)
        
        assert isinstance(result, dict), "Should return a dictionary"
        assert 'status' in result, "Result should have status field"
        
        # If successful, should have grade and breakdown
        if result['status'] == 'success':
            assert 'grade' in result, "Successful result should have grade"
            assert 'breakdown' in result, "Successful result should have breakdown"
            assert isinstance(result['breakdown'], dict), "Breakdown should be a dict"
            
            # Check breakdown has all criteria
            assert 'query_formation' in result['breakdown']
            assert 'results_quality' in result['breakdown']
            assert 'response_accuracy' in result['breakdown']
            assert 'response_completeness' in result['breakdown']


class TestMultipleResponseScenarios:
    """Test scenarios with multiple responses"""
    
    def test_response_numbering_format(self):
        """Test that response numbering is correct in the workflow"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        post_step = None
        for step in job['steps']:
            if step.get('name') == 'Post grades to issue':
                post_step = step
                break
        
        script = post_step['with']['script']
        
        # Should show "Response 1/2" format when multiple responses exist
        assert 'Response ${i+1}/${allGrades.length}' in script
        
        # Should only show numbering when multiple responses exist
        assert 'allGrades.length > 1' in script
    
    def test_highest_grade_label_logic(self):
        """Test that the highest grade label is applied to the issue"""
        workflow_path = '.github/workflows/response-grader.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        job = workflow['jobs']['grade-response']
        post_step = None
        for step in job['steps']:
            if step.get('name') == 'Post grades to issue':
                post_step = step
                break
        
        script = post_step['with']['script']
        
        # Should track the highest grade label
        assert 'highestGradeLabel' in script
        assert 'grade-excellent' in script
        assert 'grade-good' in script
        assert 'grade-fair' in script
        assert 'grade-low' in script
        
        # Should have logic to compare grades
        assert 'gradeValues' in script
