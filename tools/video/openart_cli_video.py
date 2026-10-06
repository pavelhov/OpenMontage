"""Explicit strict-only OpenArt subscription CLI video route.

No model or result contract is assumed. Real qualification and an active ledger
reservation are required; collection is an original-attempt account operation.
"""
from lib import openart_jobs as jobs
from lib.production_execution import active_openart_dispatch, ProductionGovernanceError
from tools.base_tool import (BaseTool, ToolTier, ToolResult, RetryPolicy,
                             ExecutionMode, ToolRuntime, Determinism, ToolStatus, ResourceProfile)
from tools.provider_pricing import PriceQuoteRequired

class OpenArtCLIVideo(BaseTool):
    name = 'openart_cli_video'
    provider = 'openart_cli'
    capability = 'video_generation'
    tier = ToolTier.GENERATE
    execution_mode = ExecutionMode.ASYNC
    runtime = ToolRuntime.API
    determinism = Determinism.STOCHASTIC
    retry_policy = RetryPolicy(max_retries=0)
    dependencies = ['cmd:openart']
    resource_profile = ResourceProfile(network_required=True)
    install_instructions = 'Requires the official OpenArt CLI, qualified account/model evidence and governed credit ledger.'
    capabilities = ['text_to_video', 'image_to_video']
    supports = {'text_to_video': True, 'image_to_video': True, 'reference_image': True, 'multiple_reference_images': False,
                'first_last_frame': False, 'native_audio': False,
                'explicit_selection_only': True, 'api_key_required': False,
                'oauth_session_auth': True, 'browser_automation': False}
    input_schema = {'type':'object','additionalProperties':False,'properties':{
        'prompt':{'type':'string'}, 'model':{'type':'string'},
        'mode':{'type':'string'}, 'duration':{'type':'integer'},
        'aspect_ratio':{'type':'string'}, 'resolution':{'type':'string'},
        'output_path':{'type':'string'}, 'project_dir':{'type':'string'},
        'governance':{'type':'object'}, 'operation':{'type':'string'},
        'image_path':{'type':'string'}, 'image_upload_id':{'type':'string'},
        'compiled_request_id':{'type':'string'}, 'preparation_review_id':{'type':'string'},
        'credit_authorization_id':{'type':'string'},'credit_quote_id':{'type':'string'},'credit_qualification_sha256':{'type':'string'},
        'native_dry_run_receipt_id':{'type':'string'}, 'native_dry_run_receipt_sha256':{'type':'string'}}}

    def get_status(self):
        # No CLI/auth or readiness guessing during registry discovery.
        from lib import production_execution as execution
        try:
            jobs.cli.resolve_binary()
            profiles = jobs.list_qualifications()
        except (jobs.OpenArtCLIError, ValueError, OSError, AttributeError):
            return ToolStatus.UNAVAILABLE
        if (not any(row.get('valid') is True for row in profiles)
                or execution._OPENART_COMPILED_REQUEST_CHECK is None):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def get_info(self):
        info = super().get_info()
        info.update(generation_enabled=self.get_status() == ToolStatus.AVAILABLE,
                    qualification_required=True, credit_authorization_required=True, current_quote_required=True, billing_unit='credits',
                    usd_cost_status='unknown', estimated_cost_usd=None)
        return info

    def prepare_offline(self, inputs, governance):
        from lib.openart_dispatch import offline_readiness
        return offline_readiness(inputs)

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired('OpenArt USD cost unknown; credits require retained account quote evidence')

    def execute(self, inputs):
        active = active_openart_dispatch(inputs)
        launch = jobs.launch_submit(active['root'], active['openart_binding'],
                                    active['openart_native'], active['openart_profile'],deadline=active.get('openart_deadline'))
        from lib.openart_dispatch import record_launch_result
        if jobs._RESERVATION_LOOKUP is jobs._no_ledger:
            record_launch_result(launch['attempt_id'])
        return ToolResult(success=False, cost_usd=None, model=inputs.get('model'),
            data={'provider':self.provider, 'attempt_id':launch['attempt_id'],
                  'dispatch_status':'submitted_async' if launch['status']=='submitted' else 'uncertain',
                  'openart_status':launch['status'], 'job_id_sha256':launch.get('job_id_sha256'),
                  'launch_sha256':launch.get('launch_sha256'),
                  'billing':'unknown', 'release_authorized':False})
