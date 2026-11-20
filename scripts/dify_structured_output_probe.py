# SPDX-License-Identifier: BUSL-1.1
# Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
"""Dify structured-output probe (#3233): does a workflow LLM node with structured
output work on this box's default chat model?

Runs INSIDE dify-api (piped on stdin: `docker exec -i dify-api python3 - <admin_email>`)
and walks the path a workflow LLM node walks, with Dify's own code at every step:

1. capability — the model schema the workflow adapter (`DifyPreparedLLM`) hands
   graphon must carry `ModelFeature.STRUCTURED_OUTPUT`; graphon's LLMNode refuses
   otherwise (the message below is graphon's, verbatim);
2. the call — `DifyPreparedLLM.invoke_llm_with_structured_output` with a schema that
   has a BOOLEAN field (Dify 1.18 stringified booleans, #3229);
3. the result — graphon's own `LLMNode._validate_structured_output_result`.

No app, workflow or conversation is created or read: no customer app is involved.
Bounded by an alarm. Prints exactly ONE row, `SO|<verdict>|<text>`:
  OK        the node would pass; text = the structured output
  REFUSED   Dify refuses it; text = Dify's exact error (capability / parse / validation)
  SKIP      not applicable (no default chat model; a Dify without the workflow adapter)
  ERROR     no verdict (backend down, timeout, unexpected error)
"""
import json
import os
import signal
import sys

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["answer"],
}
QUESTION = "Is water wet? Reply only with JSON that matches the schema: answer is true or false."
TIMEOUT_S = int(os.environ.get("RZFZ_DIFY_SO_PROBE_ALARM") or 75)   # the probe's own bound, seconds


def row(verdict, text=""):
    text = str(text).replace("|", "/").replace("\n", " ").replace("\r", " ")
    print("SO|%s|%s" % (verdict, text[:300]))
    sys.stdout.flush()


def _on_alarm(*_a):
    row("ERROR", "the probe timed out after %d s (a cold model load?) - no verdict" % TIMEOUT_S)
    sys.exit(3)


def main(admin_email):
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(TIMEOUT_S)
    sys.path.insert(0, "/app/api")
    try:
        from core.workflow.node_runtime import DifyPreparedLLM
        from graphon.model_runtime.entities.message_entities import UserPromptMessage
        from graphon.model_runtime.entities.model_entities import ModelFeature, ModelType
        from graphon.nodes.llm.exc import LLMNodeError
        from graphon.nodes.llm.node import LLMNode
        from core.llm_generator.output_parser.errors import OutputParserError
    except ImportError as e:
        row("SKIP", "this Dify has no workflow structured-output adapter (%s) - the probe is for Dify 1.18+" % e)
        return
    try:
        from services.model_provider.service import ModelProviderService
    except ImportError:
        from services.model_provider_service import ModelProviderService
    from app import create_app
    from core.model_manager import ModelManager
    from extensions.ext_database import db
    from models.account import Account, TenantAccountJoin

    _, app = create_app()
    with app.app_context():
        account = db.session.query(Account).filter(Account.email == admin_email).first()
        if not account:
            row("ERROR", "admin account %s not found" % admin_email)
            return
        tj = db.session.query(TenantAccountJoin).filter(TenantAccountJoin.account_id == account.id).first()
        if not tj:
            row("ERROR", "tenant not found")
            return
        d = ModelProviderService().get_default_model_of_model_type(tenant_id=tj.tenant_id, model_type="llm")
        model = getattr(d, "model", None) if d else None
        if not model:
            row("SKIP", "no default chat model in Dify")
            return
        prov = getattr(d, "provider", None)
        pname = getattr(prov, "provider", None) or getattr(prov, "provider_name", None) or ""
        try:
            from core.plugin.impl.model_runtime_factory import create_plugin_provider_manager
            mm = ModelManager(provider_manager=create_plugin_provider_manager(tenant_id=tj.tenant_id, user_id=account.id))
        except (ImportError, TypeError):
            mm = ModelManager()
        mi = mm.get_model_instance(tenant_id=tj.tenant_id, provider=pname, model_type=ModelType.LLM, model=model)
        llm = DifyPreparedLLM(mi)

        # 1. capability: graphon's LLMNode check, message verbatim
        schema = llm.get_model_schema()
        if schema.features is not None and ModelFeature.STRUCTURED_OUTPUT not in schema.features:
            row("REFUSED", "Structured output is not supported by model %s/%s (stage=capability)" % (llm.provider, llm.model_name))
            return
        # 2. the call, as the node makes it
        try:
            res = llm.invoke_llm_with_structured_output(
                prompt_messages=[UserPromptMessage(content=QUESTION)], json_schema=SCHEMA,
                model_parameters={"max_tokens": 512, "temperature": 0, "enable_thinking": False}, stop=None, stream=False)
        except OutputParserError as e:
            row("REFUSED", "Failed to parse structured output (stage=result, path=$): %s" % e)
            return
        # 3. the result, with graphon's own validator
        try:
            LLMNode._validate_structured_output_result(structured_output=res.structured_output, json_schema=SCHEMA)
        except LLMNodeError as e:
            row("REFUSED", str(e))
            return
        row("OK", json.dumps(dict(res.structured_output), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - anything else is "no verdict", never green
        row("ERROR", "%s: %s" % (type(e).__name__, e))
