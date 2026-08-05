# Dify

Dify is the workflow-automation, RAG and agentic-AI platform in your stack. You
build LLM apps visually — chatbots, agents and multi-step workflows — wiring
together prompts, your knowledge bases, tools and the models served by the
box's local GPUStack, then publish them as apps or as HTTP/OpenAI-compatible
APIs.

## How to reach it

Open [https://dify.<domain>](https://dify.<domain>) and sign in with your
razzfazz.ai single sign-on. First-time access is granted through Authentik like
every other module.

## First steps

1. From **Studio**, create an app — start from a **Chatbot** or **Agent**
   template, or an empty **Workflow** to build a pipeline node-by-node.
2. Under **Settings → Model Provider**, the box pre-wires the local models
   served by GPUStack (`https://llm.<domain>`), so you can select a chat and an
   embedding model without pasting an external API key.
3. Add a **Knowledge** base to enable RAG: upload documents, let Dify index
   them, then reference the knowledge in your app for grounded answers.
4. **Publish** the app to get a shareable web app and an API endpoint you can
   call from other services.

## Full upstream documentation

For the complete feature reference, workflow-node catalog and API details, see
the official Dify documentation: [https://docs.dify.ai/](https://docs.dify.ai/)
