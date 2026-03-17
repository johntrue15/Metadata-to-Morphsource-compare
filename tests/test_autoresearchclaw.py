"""
Tests for AutoResearchClaw workflow structure.
"""
import os
import pytest

yaml = pytest.importorskip("yaml")


class TestAutoResearchClawWorkflow:
    """Test autoresearchclaw.yml workflow structure."""

    WORKFLOW_PATH = '.github/workflows/autoresearchclaw.yml'

    def _load(self):
        with open(self.WORKFLOW_PATH, 'r') as f:
            return yaml.safe_load(f)

    def test_workflow_file_exists(self):
        assert os.path.exists(self.WORKFLOW_PATH), "autoresearchclaw.yml not found"

    def test_valid_yaml(self):
        wf = self._load()
        assert wf is not None

    def test_has_workflow_dispatch_trigger(self):
        wf = self._load()
        trigger = wf.get(True) or wf.get('on')
        assert 'workflow_dispatch' in trigger, "Must be triggered via workflow_dispatch"

    def test_workflow_dispatch_has_research_topic_input(self):
        wf = self._load()
        trigger = wf.get(True) or wf.get('on')
        inputs = trigger['workflow_dispatch']['inputs']
        assert 'research_topic' in inputs, "Must accept research_topic input"
        assert inputs['research_topic']['required'] is True

    def test_has_two_jobs(self):
        wf = self._load()
        jobs = wf['jobs']
        assert 'create-research-issue' in jobs, "Must have create-research-issue job"
        assert 'research-pipeline' in jobs, "Must have research-pipeline job"

    def test_pipeline_depends_on_issue_creation(self):
        wf = self._load()
        pipeline = wf['jobs']['research-pipeline']
        assert 'needs' in pipeline
        assert pipeline['needs'] == 'create-research-issue'

    def test_issue_creation_has_output(self):
        wf = self._load()
        job = wf['jobs']['create-research-issue']
        assert 'outputs' in job
        assert 'issue_number' in job['outputs']

    def test_pipeline_calls_research_agent(self):
        wf = self._load()
        steps = wf['jobs']['research-pipeline']['steps']
        run_steps = [s for s in steps if 'run' in s and 'research_agent.py' in s.get('run', '')]
        assert len(run_steps) >= 1, "Pipeline must call research_agent.py"

    def test_pipeline_uploads_artifact(self):
        wf = self._load()
        steps = wf['jobs']['research-pipeline']['steps']
        upload_steps = [
            s for s in steps
            if 'uses' in s and 'upload-artifact' in s.get('uses', '')
        ]
        assert len(upload_steps) >= 1, "Pipeline must upload research artifacts"
        assert upload_steps[0]['with']['name'] == 'research-report'

    def test_pipeline_posts_to_issue(self):
        wf = self._load()
        steps = wf['jobs']['research-pipeline']['steps']
        post_steps = [
            s for s in steps
            if s.get('name', '') == 'Post report to issue'
        ]
        assert len(post_steps) == 1, "Pipeline must post report to issue"
