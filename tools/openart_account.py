"""OpenArt account inspection and original-attempt recovery controls."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools import _openart_cli as cli
from tools.base_tool import (BaseTool, Determinism, DependencyError, ExecutionMode,
                             ToolResult, ToolRuntime, ToolStability, ToolTier)

READ_ONLY_ACTIONS = ("inspect", "quote", "form", "catalog", "native_surface", "cli_help", "transport_surface_probe", "native_dry_run", "readiness",
                     "status", "collect", "verify", "upload", "qualifications",
                     "qualify_inspection", "qualify_upload", "qualify_preview", "qualify_result",
                     "recover_original_submit",
                     "qualify_quote", "refresh_quote", "refresh_unknown_cost_evidence", "resolve_attempt", "repair_outbox", "qualify_resolution_contract")
DISABLED_ACTIONS = ("submit",)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class OpenArtAccount(BaseTool):
    name = "openart_account"
    version = "0.1.0"
    tier = ToolTier.ANALYZE
    capability = "provider_account"
    provider = "openart"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC if hasattr(Determinism, "STOCHASTIC") else Determinism.DETERMINISTIC
    runtime = ToolRuntime.API
    dependencies = ["cmd:openart"]
    install_instructions = ("Install the official OpenArt CLI (github.com/OpenArt-AI/cli) to PATH or "
                            "~/.local/bin/openart, or set OPENART_CLI_PATH; authenticate with `openart` OAuth.")
    side_effects = ["runs nonspending OpenArt inspection and dry-run commands",
                    "collects outputs into the original project",
                    "uploads approved references only when a nonspending contract is qualified",
                    "writes private receipts outside the checkout"]
    best_for = ["inspecting OpenArt account controls, original jobs and nonspending quotes"]
    not_good_for = ["submitting video generation"]
    input_schema = {'type': 'object',
     'required': ['action', 'read_only'],
     'properties': {'action': {'type': 'string',
                               'enum': ['inspect',
                                        'quote',
                                        'form', 'catalog', 'native_surface', 'cli_help', 'transport_surface_probe',
                                        'native_dry_run',
                                        'readiness',
                                        'status',
                                        'collect',
                                        'verify',
                                        'upload',
                                        'qualifications',
                                        'resolve_attempt',
                                        'submit',
                                        'qualify_inspection',
                                        'qualify_upload',
                                        'qualify_preview',
                                        'recover_original_submit',
                                        'qualify_result','qualify_quote','refresh_quote','refresh_unknown_cost_evidence','repair_outbox','qualify_resolution_contract'],
                               'description': 'No action submits generation or reserves credits. '
                                              'collect requires the original attempt, project '
                                              'directory and frozen request digest. upload is allowed '
                                              'only after internal nonspending qualification and '
                                              'approval lookup. resolve_attempt repairs only original evidence; submit remains '
                                              'unavailable. refresh_unknown_cost_evidence retains observations only: '
                                              'unknown requested charge never means zero, a USD estimate, or a cost ceiling.'},
                    'read_only': {'const': True, 'description': 'Must be explicitly true.'},
                    'model': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'},
                    'mode': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'},
                    'form_receipt_id': {'type': 'string'},
                    'form_receipt_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                    'cli_version_receipt_id': {'type': 'string'},
                    'cli_version_receipt_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                    'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                    'duration': {'type': 'integer', 'minimum': 1, 'maximum': 120},
                    'aspect_ratio': {'type': 'string', 'pattern': '^[0-9]{1,2}:[0-9]{1,2}$'},
                    'resolution': {'type': 'string'},
                    'probe': {'type': 'boolean',
                              'description': 'readiness only: run `openart version`.'},
                    'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 300},
                    'attempt_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'},
                    'project_dir': {'type': 'string', 'minLength': 1},
                    'project_root': {'type': 'string', 'minLength': 1},
                    'request_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
                    'upload_id': {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'},
                    'source_path': {'type': 'string', 'minLength': 1},
                    'image_upload_id': {'type': 'string',
                                        'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'},
                    'request': {'type':'object'},'quote_contract':{'type':'object'},'billing_contract':{'type':'object'},'billing_proof_id':{'type':'string','pattern':'^[0-9a-f]{64}$'},
                    'qualification_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},'quote_id':{'type':'string'},
                    'json_paths': {'type': 'object'},
                    'guarantee': {'type': 'object'},
                    'url_hosts': {'type': 'array', 'items': {'type': 'string'}}},
     'additionalProperties': False,
     'allOf': [{'if': {'properties': {'action': {'const': 'native_surface'}}},
                'then': {'required': ['model', 'mode', 'form_receipt_id', 'form_receipt_sha256',
                                      'cli_version_receipt_id', 'cli_version_receipt_sha256']}},
               {'if': {'properties': {'action': {'const': 'qualify_inspection'}}},
                'then': {'required': ['model', 'mode', 'json_paths']}},
               {'if': {'properties': {'action': {'const': 'qualify_upload'}}},
                'then': {'required': ['model', 'mode', 'json_paths', 'guarantee', 'url_hosts']}},
               {'if': {'properties': {'action': {'const': 'qualify_preview'}}},
                'then': {'required': ['model', 'mode', 'prompt']}},
               {'if': {'properties': {'action': {'const': 'qualify_result'}}},
                'then': {'required': ['attempt_id', 'json_paths']}},
               {'if': {'properties': {'action': {'const': 'recover_original_submit'}}},
                'then': {'required': ['attempt_id', 'json_paths']}},
               {'if': {'properties': {'action': {'const': 'refresh_unknown_cost_evidence'}}},
                'then': {'required': ['request'], 'not': {'anyOf': [
                    {'required': ['quote_contract']}, {'required': ['qualification_sha256']},
                    {'required': ['quote_id']}]}}}]}

    def check_dependencies(self) -> None:
        try:
            cli.resolve_binary()
        except cli.OpenArtCLIError as exc:
            raise DependencyError(f"{exc.message}. {self.install_instructions}")

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def _ok(self, action: str, evidence: Any) -> ToolResult:
        return ToolResult(success=True, cost_usd=0.0, data={
            "action": action, "provider": "openart", "reservations": 0,
            "paid_submission": False, "generation_enabled": False,
            "evidence": cli.redact(evidence)})

    @staticmethod
    def _call(argv: list[str], inputs: dict) -> dict:
        out = cli.run_readonly(argv, timeout=cli.validate_timeout(inputs.get("timeout_seconds")))
        return {"argv": cli.redact(out["argv"]), "public": cli.redact(out["public"]),
                "receipt_id": out["receipt_id"], "receipt_sha256": out["receipt_sha256"],
                "stdout_sha256": out["stdout_sha256"], "_parsed": out["parsed"]}

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        action = inputs.get("action")
        if action in DISABLED_ACTIONS:
            return self._error("unavailable", f"openart_account action {action!r} is not available")
        if action not in READ_ONLY_ACTIONS:
            return self._error("invalid_action", f"unknown openart_account action {action!r}")
        if inputs.get("read_only") is not True:
            return self._error("read_only_required", "openart_account requires explicit read_only: true")
        try:
            return self._dispatch(action, inputs)
        except cli.OpenArtCLIError as exc:
            if action == "recover_original_submit":
                return self._error(exc.kind, "original submit recovery refused; original evidence remains held")
            return self._error(exc.kind, exc.message, exc.public())
        except Exception as exc:
            if action == "recover_original_submit":
                return self._error(type(exc).__name__, "original submit recovery failed; diagnostics remain private")
            # Keep unexpected helper diagnostics useful without publishing credentials,
            # signed URL queries, or any unredacted nested value.
            public_error = cli.redact({"kind": type(exc).__name__, "message": str(exc)})
            return self._error(public_error["kind"], public_error["message"], public_error)

    @staticmethod
    def _error(kind: Any, message: Any, details: Any = None) -> ToolResult:
        public = cli.redact({"kind": kind, "message": message, "details": details or {}})
        return ToolResult(success=False, error=f"{public['kind']}: {public['message']}",
                          data={"error": public, "reservations": 0, "paid_submission": False})

    def _dispatch(self, action: str, inputs: dict) -> ToolResult:
        if action in {'cli_help', 'transport_surface_probe'}:
            allowed = {'action', 'read_only', 'timeout_seconds'}
            if action == 'transport_surface_probe':
                allowed |= {'model', 'duration', 'resolution'}
            if set(inputs) - allowed:
                raise cli.OpenArtCLIError('invalid_argument', 'schema probe accepts no caller prompt, URL, file, upload or native body')
            timeout = cli.validate_timeout(inputs.get('timeout_seconds'))
            observed = (cli.readonly_video_help(timeout=timeout) if action == 'cli_help' else
                        cli.readonly_transport_surface_probe(model=inputs.get('model'),
                            duration=inputs.get('duration'), resolution=inputs.get('resolution'), timeout=timeout))
            public = {key: observed[key] for key in ('receipt_id', 'receipt_sha256', 'stdout_sha256', 'public') if key in observed}
            return self._ok(action, {**public, 'schema_only': True, 'unqualified_for_dispatch': True,
                                    'production_ready': False})
        if action in {'catalog', 'form', 'native_surface'}:
            from lib import openart_catalog as discovery
            from lib.openart_controls import mode_capabilities
            allowed = {'action', 'read_only', 'timeout_seconds'}
            if action != 'catalog':
                allowed |= {'model', 'mode'}
            if action == 'native_surface':
                allowed |= {'form_receipt_id', 'form_receipt_sha256',
                            'cli_version_receipt_id', 'cli_version_receipt_sha256'}
            if set(inputs) - allowed:
                raise cli.OpenArtCLIError('invalid_argument', 'discovery accepts only exact model/mode and receipt selections; no caller body or URL')
            if action == 'native_surface':
                model, mode = cli._ident(inputs.get('model'), 'model'), cli._ident(inputs.get('mode'), 'mode')
                form_ref = {'receipt_id': inputs.get('form_receipt_id'), 'receipt_sha256': inputs.get('form_receipt_sha256')}
                parsed = discovery._record(form_ref, ['model', 'form', model, mode])
                version_ref = {'receipt_id': inputs.get('cli_version_receipt_id'), 'receipt_sha256': inputs.get('cli_version_receipt_sha256')}
                version = discovery._record(version_ref, ['version'])
                caps = mode_capabilities(parsed, model=model, mode=mode, cli_version=version.get('version'),
                                         element_types=discovery.observed_element_types(model, mode))
                return self._ok(action, {'form_receipt': form_ref, 'version_receipt': version_ref,
                    'native_capabilities': discovery._public_caps(caps),
                    'qualification': 'observed_not_dispatch_authority', 'production_ready': False})
            version = self._call(['version'], inputs)
            version_parsed = version.pop('_parsed')
            if action == 'catalog':
                call = self._call(['model', 'list'], inputs)
                call.pop('_parsed')
                publication = discovery.retain_discovery_observation(catalog=call, cli_version_receipt=version)
                return self._ok(action, {**publication, 'catalog_receipt': {k: call[k] for k in ('receipt_id', 'receipt_sha256')},
                    'version_receipt': {k: version[k] for k in ('receipt_id', 'receipt_sha256')},
                    'observed_candidates': discovery.read_observed_catalog(), 'production_ready': False})
            model, mode = cli._ident(inputs.get('model'), 'model'), cli._ident(inputs.get('mode'), 'mode')
            call = self._call(cli.model_form_argv(model, mode), inputs)
            parsed = call.pop('_parsed')
            root = cli.form_root_schema(parsed, model=model, mode=mode)
            publication = discovery.retain_discovery_observation(form={**call, 'model': model, 'mode': mode}, cli_version_receipt=version)
            try:
                caps = mode_capabilities(parsed, model=model, mode=mode, cli_version=version_parsed.get('version'),
                                         element_types=discovery.observed_element_types(model, mode))
                controls = discovery._public_caps(caps)
            except cli.OpenArtCLIError as exc:
                controls = {'reason': exc.kind, 'transport_supported': None}
            return self._ok(action, {**publication, 'form_receipt': {k: call[k] for k in ('receipt_id', 'receipt_sha256')},
                'version_receipt': {k: version[k] for k in ('receipt_id', 'receipt_sha256')},
                'controls': controls, 'form_shape': root['union'] or 'json_schema',
                'qualification': 'unqualified_for_dispatch', 'production_ready': False})
        if action == "recover_original_submit":
            from lib import openart_jobs as jobs
            return self._ok(action, jobs.recover_original_submit(
                self._attempt_id(inputs), json_paths=inputs.get("json_paths"),
                timeout=cli.validate_timeout(inputs.get("timeout_seconds"))))
        if action in {'qualify_quote','refresh_quote','refresh_unknown_cost_evidence','resolve_attempt','repair_outbox','qualify_resolution_contract'}:
            if action == 'refresh_unknown_cost_evidence' and any(
                    key in inputs for key in ('quote_contract', 'qualification_sha256', 'quote_id')):
                raise cli.OpenArtCLIError('invalid_argument', 'unknown-cost evidence cannot include exact quote fields')
            from lib import openart_credit as credit, openart_jobs as jobs, openart_dispatch as dispatch
            timeout=cli.validate_timeout(inputs.get('timeout_seconds'))
            if action in {'resolve_attempt','repair_outbox','qualify_resolution_contract'}:
                root=Path(inputs.get('project_dir') or inputs.get('project_root')).resolve()
                attempt=self._attempt_id(inputs)
                if action=='resolve_attempt':
                    result=dispatch.resolve_attempt(root,attempt,inputs.get('request_sha256'),timeout=timeout,billing_proof_id=inputs.get('billing_proof_id'))
                elif action=='qualify_resolution_contract':
                    result=dispatch.qualify_resolution_contract(root,attempt,inputs.get('request_sha256'),dispatch.BillingContract(**inputs.get('billing_contract',{})),timeout=timeout)
                else:
                    from lib.production_execution import _lock
                    with _lock(root): result=dispatch.repair_outbox(root,attempt)
            else:
                request=inputs.get('request')
                if not isinstance(request,dict): raise cli.OpenArtCLIError('invalid_argument','exact native request required')
                profile=jobs.load_qualification(model=request.get('model'),mode=request.get('mode'),require='pre_submit')
                if action=='qualify_quote':
                    result=credit.qualify_quote_contract(request,profile,credit.QuoteContract(**inputs.get('quote_contract',{})),timeout=timeout)
                elif action == 'refresh_unknown_cost_evidence':
                    result=credit.refresh_unknown_cost_evidence(request,profile,timeout=timeout)
                else:
                    result=credit.refresh_credit_evidence(request,profile,qualification_sha256=inputs.get('qualification_sha256'),
                        approved_quote_id=inputs.get('quote_id'),timeout=timeout)
            return self._ok(action,result)
        if action.startswith("qualify_"):
            from lib import openart_setup as setup, openart_jobs as jobs
            timeout = cli.validate_timeout(inputs.get("timeout_seconds"))
            if action == "qualify_result":
                return self._ok(action, jobs.promote_result_contract(
                    self._attempt_id(inputs), json_paths=inputs.get("json_paths"), timeout=timeout))
            model, mode = cli._ident(inputs.get("model"), "model"), cli._ident(inputs.get("mode"), "mode")
            if action == "qualify_inspection":
                result = setup.inspect_qualification(model, mode, json_paths=inputs.get("json_paths"), timeout=timeout)
            elif action == "qualify_upload":
                result = setup.qualify_upload_guarantee(model, mode, guarantee=inputs.get("guarantee"),
                    json_paths=inputs.get("json_paths"), url_hosts=inputs.get("url_hosts"), timeout=timeout)
            else:
                result = setup.qualify_preview(model, mode, prompt=inputs.get("prompt"),
                    duration=inputs.get("duration"), aspect_ratio=inputs.get("aspect_ratio"),
                    resolution=inputs.get("resolution"), image_upload_id=inputs.get("image_upload_id"), timeout=timeout)
            return self._ok(action, result)
        if action == "readiness":
            return self._ok(action, cli.readiness(probe=bool(inputs.get("probe"))))
        if action == "inspect":
            version = self._call(["version"], inputs)
            account = self._call(["account"], inputs)
            for part in (version, account):
                part.pop("_parsed")
            return self._ok(action, {"version": version, "account": account,
                                     "qualification": "unqualified_account_identity"})
        if action == "quote":
            call = self._call(cli.model_cost_argv(inputs.get("model"), inputs.get("mode")), inputs)
            call.pop("_parsed")
            return self._ok(action, {**call, "kind": "model_cost", "model": inputs["model"],
                                     "mode": inputs["mode"], "covers_settings": False,
                                     "qualification": "unqualified_for_dispatch"})
        if action == "status":
            from lib import openart_jobs as jobs
            return self._ok(action, jobs.reconcile_job(self._attempt_id(inputs)))
        if action == "collect":
            from lib import production_execution
            aid = self._attempt_id(inputs)
            project_dir = self._existing_directory(inputs.get("project_dir"), "project_dir")
            digest = inputs.get("request_sha256")
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                raise cli.OpenArtCLIError("invalid_argument", "request_sha256 must be a lowercase SHA-256 digest")
            result = production_execution.collect_openart_attempt(
                project_dir, aid, request_sha256=digest,
                timeout=cli.validate_timeout(inputs.get("timeout_seconds")))
            return self._ok(action, result)
        if action == "verify":
            from lib import openart_jobs as jobs
            aid = self._attempt_id(inputs)
            frozen = jobs.load_frozen_request(aid)
            return self._ok(action, jobs.verify_collection_receipt(aid, frozen["profile"]))
        if action == "qualifications":
            from lib import openart_jobs as jobs
            return self._ok(action, jobs.list_qualifications())
        if action == "upload":
            from lib import openart_jobs as jobs
            upload_id = cli._ident(inputs.get("upload_id"), "upload_id")
            project_root = self._existing_directory(inputs.get("project_root"), "project_root")
            source_path = Path(inputs.get("source_path", "")).expanduser().resolve()
            if not source_path.is_file():
                raise cli.OpenArtCLIError("invalid_argument", "source_path must name an existing file")
            model, mode = cli._ident(inputs.get("model"), "model"), cli._ident(inputs.get("mode"), "mode")
            result = jobs.upload_reference(project_root, upload_id, source_path,
                                           model=model, mode=mode,
                                           timeout=cli.validate_timeout(inputs.get("timeout_seconds")))
            return self._ok(action, result)

        image_url = None
        upload_id = inputs.get("image_upload_id")
        if upload_id is not None:
            from lib import openart_jobs as jobs
            model = cli._ident(inputs.get("model"), "model")
            mode = cli._ident(inputs.get("mode"), "mode")
            profile = jobs.load_qualification(model=model, mode=mode, require="inspected")
            image_url = jobs.upload_url_for(
                cli._ident(upload_id, "image_upload_id"), profile=profile,
                account_id_sha256=profile["account_id_sha256"])
        argv = cli.native_dry_run_argv(inputs.get("prompt"), model=inputs.get("model"), mode=inputs.get("mode"),
                                       duration=inputs.get("duration"), aspect_ratio=inputs.get("aspect_ratio"),
                                       resolution=inputs.get("resolution"), image_url=image_url)
        if image_url is None:
            call = self._call(argv, inputs)
        else:
            with cli.allow_image_reference():
                call = self._call(argv, inputs)
        parsed = call.pop("_parsed")
        try:
            request = cli.dry_run_request(parsed)
            request = {"endpoint": request["endpoint"], "body_sha256": request["body_sha256"],
                       "body": cli.redact(request["body"])}
        except cli.OpenArtCLIError as exc:
            request = {"error": exc.public()}
        return self._ok(action, {**call, "request": request, "qualification": "unqualified_for_dispatch"})

    @staticmethod
    def _attempt_id(inputs: dict) -> str:
        return cli._ident(inputs.get("attempt_id"), "attempt_id")

    @staticmethod
    def _existing_directory(value: Any, field: str) -> Path:
        if not isinstance(value, str) or not value:
            raise cli.OpenArtCLIError("invalid_argument", f"{field} must name an existing directory")
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise cli.OpenArtCLIError("invalid_argument", f"{field} must name an existing directory")
        return path
