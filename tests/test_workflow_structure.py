"""
Test workflow structure and configuration
"""
import yaml
import os
import pytest


class TestWorkflowStructure:
    """Test GitHub Actions workflow structure"""
    
    def test_query_processor_workflow_syntax(self):
        """Test that query-processor.yml has valid YAML syntax"""
        workflow_path = '.github/workflows/query-processor.yml'
        assert os.path.exists(workflow_path), "query-processor.yml not found"
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        assert workflow is not None, "Failed to parse workflow YAML"
        assert 'jobs' in workflow, "Workflow must have jobs"
    
    def test_query_processor_has_three_jobs(self):
        """Test that query-processor.yml has the three required jobs"""
        workflow_path = '.github/workflows/query-processor.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        jobs = workflow['jobs']
        assert 'query-formatter' in jobs, "query-formatter job must exist"
        assert 'morphosource-api' in jobs, "morphosource-api job must exist"
        assert 'chatgpt-processing' in jobs, "chatgpt-processing job must exist"
    
    def test_job_dependencies(self):
        """Test that jobs have correct dependencies"""
        workflow_path = '.github/workflows/query-processor.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        jobs = workflow['jobs']
        
        # morphosource-api should depend on query-formatter
        assert 'needs' in jobs['morphosource-api'], "morphosource-api must have needs"
        assert jobs['morphosource-api']['needs'] == 'query-formatter', \
            "morphosource-api must depend on query-formatter"
        
        # chatgpt-processing should depend on both previous jobs
        assert 'needs' in jobs['chatgpt-processing'], "chatgpt-processing must have needs"
        needs = jobs['chatgpt-processing']['needs']
        assert isinstance(needs, list), "chatgpt-processing needs should be a list"
        assert 'query-formatter' in needs, "chatgpt-processing must depend on query-formatter"
        assert 'morphosource-api' in needs, "chatgpt-processing must depend on morphosource-api"
    
    def test_query_formatter_outputs(self):
        """Test that query-formatter job has required outputs"""
        workflow_path = '.github/workflows/query-processor.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        jobs = workflow['jobs']
        query_formatter = jobs['query-formatter']
        
        assert 'outputs' in query_formatter, "query-formatter must have outputs"
        outputs = query_formatter['outputs']
        assert 'formatted_query' in outputs, "query-formatter must output formatted_query"
        assert 'api_params' in outputs, "query-formatter must output api_params"
    
    def test_artifacts(self):
        """Test that jobs upload artifacts"""
        workflow_path = '.github/workflows/query-processor.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        jobs = workflow['jobs']
        
        # Check that each job uploads an artifact
        artifacts_to_check = [
            ('query-formatter', 'formatted-query'),
            ('morphosource-api', 'morphosource-results'),
            ('chatgpt-processing', 'chatgpt-response')
        ]
        
        for job_name, artifact_name in artifacts_to_check:
            job = jobs[job_name]
            steps = job['steps']
            
            # Find upload step
            upload_steps = [s for s in steps if 'uses' in s and 'upload-artifact' in s['uses']]
            assert len(upload_steps) > 0, f"{job_name} must upload an artifact"
            
            # Check artifact name
            upload_step = upload_steps[0]
            assert 'with' in upload_step, f"{job_name} upload step must have 'with' config"
            assert 'name' in upload_step['with'], f"{job_name} upload step must have artifact name"
            assert upload_step['with']['name'] == artifact_name, \
                f"{job_name} must upload artifact named {artifact_name}"


    def test_scripts_are_called(self):
        """Test that jobs call Python scripts instead of inline code"""
        workflow_path = '.github/workflows/query-processor.yml'
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        jobs = workflow['jobs']
        
        # Check query-formatter calls query_formatter.py
        formatter_steps = jobs['query-formatter']['steps']
        format_step = [s for s in formatter_steps if s.get('name') == 'Format query with ChatGPT'][0]
        assert 'run' in format_step
        assert 'query_formatter.py' in format_step['run'], \
            "query-formatter should call query_formatter.py"
        
        # Check morphosource-api calls morphosource_api.py
        api_steps = jobs['morphosource-api']['steps']
        search_step = [s for s in api_steps if s.get('name') == 'Search MorphoSource'][0]
        assert 'run' in search_step
        assert 'morphosource_api.py' in search_step['run'], \
            "morphosource-api should call morphosource_api.py"
        
        # Check chatgpt-processing calls chatgpt_processor.py
        processing_steps = jobs['chatgpt-processing']['steps']
        process_step = [s for s in processing_steps if s.get('name') == 'Process with ChatGPT'][0]
        assert 'run' in process_step
        assert 'chatgpt_processor.py' in process_step['run'], \
            "chatgpt-processing should call chatgpt_processor.py"


class TestIssueQueryTrigger:
    """Test issue-query-trigger workflow"""
    
    def test_issue_trigger_workflow_syntax(self):
        """Test that issue-query-trigger.yml has valid YAML syntax"""
        workflow_path = '.github/workflows/issue-query-trigger.yml'
        assert os.path.exists(workflow_path), "issue-query-trigger.yml not found"
        
        with open(workflow_path, 'r') as f:
            workflow = yaml.safe_load(f)
        
        assert workflow is not None, "Failed to parse workflow YAML"
        # 'on' is parsed as True in YAML, so check for True key
        assert True in workflow or 'on' in workflow, "Workflow must have trigger"
        trigger = workflow.get(True) or workflow.get('on')
        assert 'issues' in trigger, "Workflow must be triggered by issues"
