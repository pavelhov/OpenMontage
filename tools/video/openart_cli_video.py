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
        'first_frame':{'type':'string'}, 'last_frame':{'type':'string'},
        'last_image_path':{'type':'string'}, 'end_image_upload_id':{'type':'string'},
        'reference_image_paths':{'type':'array','items':{'type':'string'}},
        'reference_video_paths':{'type':'array','items':{'type':'string'}},
        'reference_audio_paths':{'type':'array','items':{'type':'string'}},
        'reference_image_upload_ids':{'type':'array','items':{'type':'string'}},
        'reference_video_upload_ids':{'type':'array','items':{'type':'string'}},
        'reference_audio_upload_ids':{'type':'array','items':{'type':'string'}},
        'input_assets':{'type':'array','items':{'type':'object','additionalProperties':False,
            'required':['role','source_path','source_sha256','upload_id'], 'properties':{
                'role':{'enum':['first_frame','last_frame','reference_image','reference_video','reference_audio']},
                'source_path':{'type':'string'}, 'source_sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'},
                'upload_id':{'type':'string'}, 'body_pointer':{'type':'string'}}}},
        'native_params':{'type':'object'},
        'compiled_request_id':{'type':'string'}, 'preparation_review_id':{'type':'string'},
        'credit_authorization_id':{'type':'string'},'credit_quote_id':{'type':'string'},'credit_qualification_sha256':{'type':'string'},
        'unknown_cost_authorization_id':{'type':'string','minLength':1,
            'description':'Retained explicit unknown-cost authorization; requires matching unknown_cost_evidence_id. Unknown never means zero, a USD estimate, or a cost ceiling.'},
        'unknown_cost_evidence_id':{'type':'string','pattern':'^[0-9a-f]{64}$'},
        'native_dry_run_receipt_id':{'type':'string'}, 'native_dry_run_receipt_sha256':{'type':'string'}},
        'dependentRequired': {'unknown_cost_authorization_id':['unknown_cost_evidence_id'],
                              'unknown_cost_evidence_id':['unknown_cost_authorization_id']},
        'allOf':[{'if':{'anyOf':[{'required':['unknown_cost_authorization_id']},
                                {'required':['unknown_cost_evidence_id']}]},
                  'then':{'not':{'anyOf':[{'required':['credit_authorization_id']},
                                         {'required':['credit_quote_id']},
                                         {'required':['credit_qualification_sha256']}]}}}]}

    def get_status(self):
        # No CLI/auth or readiness guessing during registry discovery.
        from lib import production_execution as execution
        try:
            jobs.cli.resolve_binary()
            profiles = jobs.list_qualifications()
        except (jobs.OpenArtCLIError, ValueError, OSError, AttributeError):
            return ToolStatus.UNAVAILABLE
        if (not any(row.get('production_ready') is True for row in profiles)
                or execution._OPENART_COMPILED_REQUEST_CHECK is None):
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def get_info(self):
        info = super().get_info()
        info.update(generation_enabled=self.get_status() == ToolStatus.AVAILABLE,
                    qualification_required=True, credit_authorization_required=True, current_quote_required=True, billing_unit='credits',
                    usd_cost_status='unknown', estimated_cost_usd=None,
                    default_authorization_mode='exact_credit', requirement_flags_apply_to='exact_credit',
                    authorization_modes=self._authorization_modes())
        info['model_catalog'] = self._model_catalog()
        modes = [mode for model in info['model_catalog'].values() for mode in model['modes'].values()]
        info['supports'] = {**info['supports'],
            'first_last_frame': any(mode.get('first_last_frame') is True for mode in modes),
            'native_audio': any(mode.get('native_audio') is True for mode in modes),
            'multiple_reference_images': any(mode.get('multiple_reference_images') is True for mode in modes),
            'control_scope': 'per_verified_model_mode'}
        from lib.openart_catalog import read_observed_catalog
        info['observed_candidates'] = read_observed_catalog()
        return info

    @staticmethod
    def _authorization_modes(native_mode=None):
        """Discovery distinguishes the legacy default from explicit unknown cost."""
        modes = {
            'exact_credit': {'default': True, 'current_quote_required': True,
                             'credit_authorization_required': True},
            'unknown_cost': {'default': False, 'recommended': False,
                             'current_quote_required': False, 'credit_authorization_required': False,
                             'unknown_cost_authorization_required': True,
                             'required_fields': ['unknown_cost_authorization_id', 'unknown_cost_evidence_id'],
                             'native_modes': ['text2video', 'image2video'],
                             'reference_mode': 'reference_free_or_approved_source_bound',
                             'source_binding_required_for_references': True,
                             'explicit_acknowledgement': 'no_enforceable_credit_ceiling',
                             'guaranteed_ceiling': False, 'requested_charge': 'unknown',
                             # Supported engine mode only: requires a separately approved,
                             # active unknown-cost policy variant; never implies an
                             # active policy, account readiness or qualification.
                             'auto_continue_available': True,
                             'auto_continue_requires': 'active_policy_openart_unknown_cost_variant',
                             'auto_continue_policy_active': None},
        }
        if native_mode is not None:
            if native_mode not in {'text2video', 'image2video'}:
                modes.pop('unknown_cost')
            else:
                modes['unknown_cost']['native_modes'] = [native_mode]
                modes['unknown_cost']['reference_mode'] = 'source_bound' if native_mode == 'image2video' else 'reference_free'
        return modes

    # Creative controls whose effective preview/default values are safe to show in a
    # menu. Everything else (prompt, signed image URL, unknown params) is hashed only.
    _SAFE_PREVIEW_PARAMS = frozenset({'duration', 'aspectRatio', 'resolution'})

    _SAFE_STRING_PATTERNS = {
        'aspectRatio': r'\d{1,2}:\d{1,2}',
        'resolution': r'\d{3,4}[pP]|[1248][kK]',
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
    def _public_capabilities(caps):
        """Keep binding facts public while protecting provider default/enum bytes."""
        def walk(value, name=None):
            if isinstance(value, list):
                return [walk(item, name) for item in value]
            if not isinstance(value, dict):
                return value
            result = {}
            for key, child in value.items():
                if key in ('default', 'const', 'examples', 'example', 'description', 'title', 'pattern', 'url') and child is not None:
                    result[key] = OpenArtCLIVideo._safe_value(name, child)
                elif key == 'enum' and isinstance(child, list):
                    result[key] = [OpenArtCLIVideo._safe_value(name, item) for item in child]
                elif key == 'params' and isinstance(child, dict):
                    result[key] = {native: walk(spec, native) for native, spec in child.items()}
                else:
                    result[key] = walk(child, name)
            return result
        from lib.openart_controls import public_capabilities
        return walk(public_capabilities(caps))

    @staticmethod
    def _model_catalog():
        """Read-only exact catalog from verified, production-ready profiles.

        Production-ready profiles retain only receipt references {kind, receipt_id, receipt_sha256};
        the `model form` and exact dry-run preview are loaded through the trusted
        qualification record path (digest, 0600 perms, retained stdout stream
        integrity). No OpenArt CLI call, no ledger, no row mutation. Prompt, image
        URL and any non-creative values are never exposed (sha256 only). A control
        present in argv but absent from the effective preview is unqualified, never
        guessed. Inspected-only, fixture or unreadable profiles never appear. A
        prior result proof remains optional empirical evidence.
        """
        from tools import _openart_cli as cli
        catalog = {}
        try:
            rows = jobs.list_qualifications()
        except Exception:
            return catalog
        for row in rows:
            if row.get('source') != 'real' or row.get('production_ready') is not True:
                continue
            try:
                prof = jobs.load_qualification(model=row['model'], mode=row['mode'], require='production_ready')
                if prof['profile_sha256'] != row.get('profile_sha256') or prof.get('source') != 'real':
                    continue
                refs = {e.get('kind'): e for e in prof.get('captured_receipts') or [] if isinstance(e, dict)}
                form_rec = jobs._qual_record(refs['form'], 'form', 'generation_unqualified')
                dry_rec = jobs._qual_record(refs['dry_run'], 'dry_run', 'generation_unqualified')
                form_root = cli.form_root_schema(form_rec.get('parsed'), model=row['model'], mode=row['mode'])
                dry = cli.dry_run_request(dry_rec.get('parsed'))
                if dry['endpoint'] != prof.get('dry_run_endpoint'):
                    continue
                preview = dry['body'].get('params')
                if not isinstance(preview, dict):
                    continue
                if form_root['union']:
                    from jsonschema import Draft202012Validator
                    matches = [branch for branch in form_root['branches']
                               if not list(Draft202012Validator(branch).iter_errors(preview))]
                    if len(matches) != 1:
                        continue  # an exact catalog preview cannot pick an ambiguous branch
                    form = cli.form_controls(matches[0])
                else:
                    form = cli.form_controls(form_root['branches'][0])
            except Exception:
                continue
            from lib.openart_controls import mode_capabilities, ROLES
            try:
                caps = mode_capabilities(form_rec.get('parsed'), model=row['model'], mode=row['mode'], cli_version=prof.get('cli_version', '0.1.1'))
            except cli.OpenArtCLIError:
                # Saved legacy qualification remains usable under its original
                # exact preview; an unknown transport never establishes new roles.
                caps = {'roles': {role: {'supported': False, 'reason': 'transport_unknown'} for role in ROLES},
                        'params': {}, 'unreachable': False, 'transport_status': 'unknown_legacy_qualified_preview'}
            caps = OpenArtCLIVideo._public_capabilities(caps)
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
                'level': row.get('level'), 'source': 'real',
                'operation': {'image2video': 'image_to_video', 'element2video': 'reference_to_video', 'text2video': 'text_to_video'}.get(row['mode']),
                'cli_version': prof.get('cli_version'), 'tier': prof.get('tier'),
                'account_id_sha256': prof.get('account_id_sha256'),
                'profile_sha256': prof['profile_sha256'], 'form_sha256': prof.get('form_sha256'),
                'preview_body_sha256': dry['body_sha256'],
                'result_contract_sha256': row.get('result_contract_sha256'),
                'result_proof_id': row.get('result_proof_id'),
                'production_ready': True,
                'full_result_qualified': row.get('full_result_qualified') is True,
                'empirical_result_status': row.get('empirical_result_status', 'not_tested'),
                'native_controls': native, 'required_unqualified': unqualified_required,
                'argv_flag_without_effective_preview': argv_only,
                'native_capabilities': caps,
                'first_last_frame': caps['roles']['last_frame']['supported'],
                'native_audio': caps['params'].get('generateAudio', {}).get('binding') == 'flag',
                'multiple_reference_images': caps['roles']['reference_image']['supported'],
                'billing_unit': 'credits', 'current_quote_required': True, 'usd_cost_status': 'unknown',
                'default_authorization_mode': 'exact_credit', 'requirement_flags_apply_to': 'exact_credit',
                'authorization_modes': OpenArtCLIVideo._authorization_modes(row['mode']),
                'production_ready': True, 'fresh_prelaunch_refresh_required': True}
        return catalog

    def prepare_offline(self, inputs, governance):
        from lib.openart_dispatch import offline_readiness
        return offline_readiness(inputs)

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired('OpenArt USD cost unknown; exact-credit mode requires retained account quote evidence; unknown-cost has no enforceable credit ceiling')

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
