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
        info['model_catalog'] = self._model_catalog()
        return info

    # Creative controls whose effective preview/default values are safe to show in a
    # menu. Everything else (prompt, signed image URL, unknown params) is hashed only.
    _SAFE_PREVIEW_PARAMS = frozenset({'duration', 'aspectRatio', 'resolution'})

    _SAFE_STRING_PATTERNS = {
        'aspectRatio': r'\d{1,2}:\d{1,2}',
        'resolution': r'\d{3,4}p|[1248]k',
    }

    @staticmethod
    def _safe_value(name, value):
        """Menu-safe effective value: only allowlisted creative controls with
        finite numbers or strings matching that control's creative format are
        shown; everything else (prompt, image URL/id, unknown params) is sha256 only."""
        import hashlib, json as _json, math, re
        safe = False
        if name in OpenArtCLIVideo._SAFE_PREVIEW_PARAMS:
            if name == 'duration':
                safe = (not isinstance(value, bool) and isinstance(value, (int, float))
                        and math.isfinite(value) and 0 < value <= 600)
            else:
                pattern = OpenArtCLIVideo._SAFE_STRING_PATTERNS.get(name)
                safe = (isinstance(value, str) and pattern is not None
                        and re.fullmatch(pattern, value) is not None)
        if safe:
            return {'value': value}
        digest = hashlib.sha256(_json.dumps(value, sort_keys=True, separators=(',', ':'),
                                            default=str).encode()).hexdigest()
        return {'value_redacted': True, 'value_sha256': digest}

    @staticmethod
    def _model_catalog():
        """Read-only exact catalog from verified FULL real-result profiles only.

        Profiles retain only receipt references {kind, receipt_id, receipt_sha256};
        the `model form` and exact dry-run preview are loaded through the trusted
        qualification record path (digest, 0600 perms, retained stdout stream
        integrity). No OpenArt CLI call, no ledger, no row mutation. Prompt, image
        URL and any non-creative values are never exposed (sha256 only). A control
        present in argv but absent from the effective preview is unqualified, never
        guessed. Staged, fixture or unreadable profiles never appear.
        """
        from tools import _openart_cli as cli
        catalog = {}
        try:
            rows = jobs.list_qualifications()
        except Exception:
            return catalog
        for row in rows:
            if row.get('source') != 'real' or row.get('valid') is not True or row.get('level') != 'full':
                continue
            try:
                prof = jobs.load_qualification(model=row['model'], mode=row['mode'], require='full')
                if prof['profile_sha256'] != row.get('profile_sha256') or prof.get('source') != 'real':
                    continue
                refs = {e.get('kind'): e for e in prof.get('captured_receipts') or [] if isinstance(e, dict)}
                form_rec = jobs._qual_record(refs['form'], 'form', 'generation_unqualified')
                dry_rec = jobs._qual_record(refs['dry_run'], 'dry_run', 'generation_unqualified')
                form = cli.form_controls(form_rec.get('parsed'))
                dry = cli.dry_run_request(dry_rec.get('parsed'))
                if dry['endpoint'] != prof.get('dry_run_endpoint'):
                    continue
                preview = dry['body'].get('params')
                if not isinstance(preview, dict):
                    continue
            except Exception:
                continue
            native = {}
            for name, spec in form.items():
                in_preview = name in preview
                ctl = {'observed_in_form': True, 'type': spec.get('type'),
                       'required': spec.get('required'), 'qualified_in_exact_preview': in_preview}
                if name in OpenArtCLIVideo._SAFE_PREVIEW_PARAMS:
                    enum = spec.get('enum')
                    # Every member passes the same creative format/range gate as
                    # preview values; malformed members are hashed, never echoed.
                    ctl['enum'] = ([OpenArtCLIVideo._safe_value(name, v) for v in enum]
                                   if isinstance(enum, list) else None)
                    if spec.get('default') is not None:
                        ctl['form_default'] = OpenArtCLIVideo._safe_value(name, spec.get('default'))
                if in_preview:
                    ctl['preview'] = OpenArtCLIVideo._safe_value(name, preview[name])
                native[name] = ctl
            for name in sorted(set(preview) - set(form)):
                native[name] = {'observed_in_form': False, 'qualified_in_exact_preview': True,
                                'preview': OpenArtCLIVideo._safe_value(name, preview[name])}
            argv = dry_rec.get('argv') or []
            argv_only = sorted({a[2:].replace('-', '_') for a in argv if isinstance(a, str) and a.startswith('--')}
                               & {'resolution'} - {n for n in preview})
            unqualified_required = sorted(n for n, c in native.items()
                                          if c.get('required') and not c['qualified_in_exact_preview'])
            entry = catalog.setdefault(row['model'], {'provider': 'openart_cli', 'modes': {}})
            entry['modes'][row['mode']] = {
                'level': 'full', 'source': 'real',
                'operation': 'image_to_video' if row['mode'] == 'image2video' else 'text_to_video',
                'cli_version': prof.get('cli_version'), 'tier': prof.get('tier'),
                'account_id_sha256': prof.get('account_id_sha256'),
                'profile_sha256': prof['profile_sha256'], 'form_sha256': prof.get('form_sha256'),
                'preview_body_sha256': dry['body_sha256'],
                'result_contract_sha256': row.get('result_contract_sha256'),
                'result_proof_id': row.get('result_proof_id'),
                'native_controls': native, 'required_unqualified': unqualified_required,
                'argv_flag_without_effective_preview': argv_only,
                'first_last_frame': False, 'native_audio': False, 'multiple_reference_images': False,
                'billing_unit': 'credits', 'current_quote_required': True, 'usd_cost_status': 'unknown',
                'production_ready': True, 'fresh_prelaunch_refresh_required': True}
        return catalog

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
