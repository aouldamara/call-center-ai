from typing import List, Optional
from azure.communication.callautomation import (
    CallAutomationClient,
    CallConnectionClient,
    FileSource,
    PhoneNumberIdentifier,
    RecognizeInputType,
    SsmlSource,
)
from azure.communication.sms import SmsClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.core.messaging import CloudEvent
from azure.eventgrid import EventGridEvent, SystemEventNames
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from datetime import datetime
from enum import Enum
from fastapi import FastAPI, status, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse
from helpers.config import CONFIG
from helpers.logging import build_logger
from helpers.prompts import LLM as LLMPrompt, TTS as TTSPrompt, Sounds as SoundPrompt
from jinja2 import Environment, FileSystemLoader, select_autoescape
from models.action import ActionModel, Indent as IndentAction
from models.reminder import ReminderModel
from helpers.config_models.database import Mode as DatabaseMode
from persistence.sqlite import SqliteStore
from persistence.cosmos import CosmosStore
from pydantic.json import pydantic_encoder
import asyncio
from models.call import (
    CallModel,
    MessageModel as CallMessageModel,
    Persona as CallPersona,
    ToolModel as CallToolModel,
)
from models.claim import ClaimModel
from openai import AsyncAzureOpenAI
from uuid import UUID
import json


_logger = build_logger(__name__)
_logger.info(f"claim-ai v{CONFIG.version}")

jinja = Environment(
    autoescape=select_autoescape(),
    enable_async=True,
    loader=FileSystemLoader("public_website"),
)
_logger.info(f"Using OpenAI GPT model {CONFIG.openai.gpt_model}")
oai_gpt = AsyncAzureOpenAI(
    api_version="2023-12-01-preview",
    azure_deployment=CONFIG.openai.gpt_deployment,
    azure_endpoint=CONFIG.openai.endpoint,
    # Authentication, either RBAC or API key
    api_key=CONFIG.openai.api_key.get_secret_value() if CONFIG.openai.api_key else None,
    azure_ad_token_provider=(
        get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        if not CONFIG.openai.api_key
        else None
    ),
)
source_caller = PhoneNumberIdentifier(CONFIG.communication_service.phone_number)
_logger.info(f"Using phone number {str(CONFIG.communication_service.phone_number)}")
# Cannot place calls with RBAC, need to use access key (see: https://learn.microsoft.com/en-us/azure/communication-services/concepts/authentication#authentication-options)
call_automation_client = CallAutomationClient(
    endpoint=CONFIG.communication_service.endpoint,
    credential=AzureKeyCredential(
        CONFIG.communication_service.access_key.get_secret_value()
    ),
)
sms_client = SmsClient(
    credential=DefaultAzureCredential(), endpoint=CONFIG.communication_service.endpoint
)
db = (
    SqliteStore(CONFIG.database.sqlite)
    if CONFIG.database.mode == DatabaseMode.SQLITE
    else CosmosStore(CONFIG.database.cosmos_db)
)
_logger.info(f'Using root path "{CONFIG.api.root_path}"')
api = FastAPI(
    contact={
        "url": "https://github.com/clemlesne/claim-ai-phone-bot",
    },
    description="AI-powered call center solution with Azure and OpenAI GPT.",
    license_info={
        "name": "Apache-2.0",
        "url": "https://github.com/clemlesne/claim-ai-phone-bot/blob/master/LICENCE",
    },
    root_path=CONFIG.api.root_path,
    title="claim-ai-phone-bot",
    version=CONFIG.version,
)


CALL_EVENT_URL = f'{CONFIG.api.events_domain.strip("/")}/call/event'
CALL_INBOUND_URL = f'{CONFIG.api.events_domain.strip("/")}/call/inbound'


class Context(str, Enum):
    TRANSFER_FAILED = "transfer_failed"
    CONNECT_AGENT = "connect_agent"
    GOODBYE = "goodbye"


@api.get(
    "/health/liveness",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Liveness healthckeck, always returns 204, used to check if the API is up.",
)
async def health_liveness_get() -> None:
    pass


@api.get(
    "/call/report/{call_id}",
    description="Display the call report in a web page.",
)
async def call_report_get(call_id: UUID) -> HTMLResponse:
    call = await db.call_aget(call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {call_id} not found",
        )

    template = jinja.get_template("report.html")
    render = await template.render_async(
        bot_company=CONFIG.workflow.bot_company,
        bot_name=CONFIG.workflow.bot_name,
        call=call,
    )
    return HTMLResponse(content=render)


@api.get(
    "/call",
    description="Get all calls by phone number.",
)
async def call_get(phone_number: str) -> List[CallModel]:
    return await db.call_asearch_all(phone_number) or []


@api.get(
    "/call/initiate",
    status_code=status.HTTP_204_NO_CONTENT,
    description="Initiate an outbound call to a phone number.",
)
async def call_initiate_get(phone_number: str) -> None:
    _logger.info(f"Initiating outbound call to {phone_number}")
    call_connection_properties = call_automation_client.create_call(
        callback_url=await callback_url(phone_number),
        cognitive_services_endpoint=CONFIG.cognitive_service.endpoint,
        source_caller_id_number=source_caller,
        target_participant=PhoneNumberIdentifier(phone_number),
    )
    _logger.info(
        f"Created call with connection id: {call_connection_properties.call_connection_id}"
    )


@api.post(
    "/call/inbound",
    description="Handle incoming call from a Azure Event Grid event originating from Azure Communication Services.",
)
async def call_inbound_post(request: Request):
    for event_dict in await request.json():
        event = EventGridEvent.from_dict(event_dict)
        event_type = event.event_type

        _logger.debug(f"Call inbound event {event_type} with data {event.data}")

        if event_type == SystemEventNames.EventGridSubscriptionValidationEventName:
            validation_code = event.data["validationCode"]
            _logger.info(f"Validating Event Grid subscription ({validation_code})")
            return JSONResponse(
                content={"validationResponse": event.data["validationCode"]},
                status_code=status.HTTP_200_OK,
            )

        elif event_type == SystemEventNames.AcsIncomingCallEventName:
            if event.data["from"]["kind"] == "phoneNumber":
                phone_number = event.data["from"]["phoneNumber"]["value"]
            else:
                phone_number = event.data["from"]["rawId"]

            _logger.debug(f"Incoming call handler caller ID: {phone_number}")
            call_context = event.data["incomingCallContext"]
            answer_call_result = call_automation_client.answer_call(
                callback_url=await callback_url(phone_number),
                cognitive_services_endpoint=CONFIG.cognitive_service.endpoint,
                incoming_call_context=call_context,
            )
            _logger.info(
                f"Answered call with {phone_number} ({answer_call_result.call_connection_id})"
            )


@api.post(
    "/call/event/{call_id}",
    description="Handle callbacks from Azure Communication Services.",
    status_code=status.HTTP_204_NO_CONTENT,
)
# TODO: Secure this endpoint with a secret
# See: https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/communication-services/how-tos/call-automation/secure-webhook-endpoint.md
async def call_event_post(
    request: Request, background_tasks: BackgroundTasks, call_id: UUID
) -> None:
    for event_dict in await request.json():
        background_tasks.add_task(communication_evnt_worker, event_dict, call_id)


async def communication_evnt_worker(event_dict: dict, call_id: UUID) -> None:
    call = await db.call_aget(call_id)
    if not call:
        _logger.warn(f"Call {call_id} not found")
        return

    event = CloudEvent.from_dict(event_dict)
    connection_id = event.data["callConnectionId"]
    operation_context = event.data.get("operationContext", None)
    client = call_automation_client.get_call_connection(
        call_connection_id=connection_id
    )
    event_type = event.type

    _logger.debug(f"Call event received {event_type} for call {call}")
    _logger.debug(event.data)

    if event_type == "Microsoft.Communication.CallConnected":  # Call answered
        _logger.info(f"Call connected ({call.call_id})")
        call.recognition_retry = 0  # Reset recognition retry counter

        if not call.messages:  # First call
            await handle_recognize_text(
                call=call,
                client=client,
                text=TTSPrompt.HELLO,
            )

        else:  # Returning call
            call.messages.append(
                CallMessageModel(
                    content="Customer called again.", persona=CallPersona.HUMAN
                )
            )
            await handle_play(
                call=call,
                client=client,
                text=TTSPrompt.WELCOME_BACK,
            )
            await intelligence(call, client)

    elif event_type == "Microsoft.Communication.CallDisconnected":  # Call hung up
        _logger.info(f"Call disconnected ({call.call_id})")
        await handle_hangup(call=call, client=client)

    elif (
        event_type == "Microsoft.Communication.RecognizeCompleted"
    ):  # Speech recognized
        if event.data["recognitionType"] == "speech":
            speech_text = event.data["speechResult"]["speech"]
            _logger.info(f"Recognition completed ({call.call_id}): {speech_text}")

            if speech_text is not None and len(speech_text) > 0:
                call.messages.append(
                    CallMessageModel(content=speech_text, persona=CallPersona.HUMAN)
                )
                await intelligence(call, client)

    elif (
        event_type == "Microsoft.Communication.RecognizeFailed"
    ):  # Speech recognition failed
        result_information = event.data["resultInformation"]
        error_code = result_information["subCode"]

        # Error codes:
        # 8510 = Action failed, initial silence timeout reached
        # 8532 = Action failed, inter-digit silence timeout reached
        # 8512 = Unknown internal server error
        # See: https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/communication-services/how-tos/call-automation/recognize-action.md#event-codes
        if (
            error_code in (8510, 8532, 8512) and call.recognition_retry < 10
        ):  # Timeout retry
            await handle_recognize_text(
                call=call,
                client=client,
                text=TTSPrompt.TIMEOUT_SILENCE,
            )
            call.recognition_retry += 1

        else:  # Timeout reached or other error
            await handle_play(
                call=call,
                client=client,
                context=Context.GOODBYE,
                text=TTSPrompt.GOODBYE,
            )

    elif event_type == "Microsoft.Communication.PlayCompleted":  # Media played
        _logger.debug(f"Play completed ({call.call_id})")

        if (
            operation_context == Context.TRANSFER_FAILED
            or operation_context == Context.GOODBYE
        ):  # Call ended
            _logger.info(f"Ending call ({call.call_id})")
            await handle_hangup(call=call, client=client)

        elif operation_context == Context.CONNECT_AGENT:  # Call transfer
            _logger.info(f"Initiating transfer call initiated ({call.call_id})")
            agent_caller = PhoneNumberIdentifier(
                str(CONFIG.workflow.agent_phone_number)
            )
            client.transfer_call_to_participant(target_participant=agent_caller)

    elif event_type == "Microsoft.Communication.PlayFailed":  # Media play failed
        _logger.debug(f"Play failed ({call.call_id})")

        result_information = event.data["resultInformation"]
        error_code = result_information["subCode"]

        # See: https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/communication-services/how-tos/call-automation/play-action.md
        if error_code == 8535:  # Action failed, file format is invalid
            _logger.warn("Error during media play, file format is invalid")
        elif error_code == 8536:  # Action failed, file could not be downloaded
            _logger.warn("Error during media play, file could not be downloaded")
        elif error_code == 9999:  # Unknown internal server error
            _logger.warn("Error during media play, unknown internal server error")
        else:
            _logger.warn(f"Error during media play, unknown error code {error_code}")

    elif (
        event_type == "Microsoft.Communication.CallTransferAccepted"
    ):  # Call transfer accepted
        _logger.info(f"Call transfer accepted event ({call.call_id})")
        # TODO: Is there anything to do here?

    elif (
        event_type == "Microsoft.Communication.CallTransferFailed"
    ):  # Call transfer failed
        _logger.debug(f"Call transfer failed event ({call.call_id})")
        result_information = event.data["resultInformation"]
        sub_code = result_information["subCode"]
        _logger.info(f"Error during call transfer, subCode {sub_code} ({call.call_id})")
        await handle_play(
            call=call,
            client=client,
            context=Context.TRANSFER_FAILED,
            text=TTSPrompt.CALLTRANSFER_FAILURE,
        )

    await db.call_aset(call)


async def intelligence(call: CallModel, client: CallConnectionClient) -> None:
    """
    Handle the intelligence of the call, including: GPT chat, GPT completion, TTS, and media play.

    Play the loading sound while waiting for the intelligence to be processed. If the intelligence is not processed after 15 seconds, play the timeout sound. If the intelligence is not processed after 30 seconds, stop the intelligence processing and play the error sound.
    """
    chat_task = asyncio.create_task(gpt_chat(call))
    soft_timeout_task = asyncio.create_task(
        asyncio.sleep(CONFIG.workflow.intelligence_soft_timeout_sec)
    )
    soft_timeout_triggered = False
    hard_timeout_task = asyncio.create_task(
        asyncio.sleep(CONFIG.workflow.intelligence_hard_timeout_sec)
    )
    chat_res = None

    try:
        while True:
            _logger.debug(f"Chat task status ({call.call_id}): {chat_task.done()}")
            # Play loading sound
            await handle_media(
                call=call,
                client=client,
                sound=SoundPrompt.LOADING,
            )
            # Break when chat coroutine is done
            if chat_task.done():
                # Clean up
                soft_timeout_task.cancel()
                hard_timeout_task.cancel()
                # Answer with chat result
                chat_res = chat_task.result()
                break
            # Break when hard timeout is reached
            if hard_timeout_task.done():
                _logger.warn(
                    f"Hard timeout of {CONFIG.workflow.intelligence_hard_timeout_sec}s reached ({call.call_id})"
                )
                # Clean up
                chat_task.cancel()
                soft_timeout_task.cancel()
                break
            # Speak when soft timeout is reached
            if soft_timeout_task.done() and not soft_timeout_triggered:
                _logger.warn(
                    f"Soft timeout of {CONFIG.workflow.intelligence_soft_timeout_sec}s reached ({call.call_id})"
                )
                soft_timeout_triggered = True
                await handle_play(
                    call=call,
                    client=client,
                    text=TTSPrompt.TIMEOUT_LOADING,
                )
            # Wait to not block the event loop and play too many sounds
            await asyncio.sleep(5)
    except Exception:
        _logger.warn(f"Error loading intelligence ({call.call_id})", exc_info=True)

    # For any error reason, answer with error
    if not chat_res:
        _logger.debug(
            f"Error loading intelligence ({call.call_id}), answering with default error"
        )
        chat_res = ActionModel(content=TTSPrompt.ERROR, intent=IndentAction.CONTINUE)

    _logger.info(f"Chat ({call.call_id}): {chat_res}")

    if chat_res.intent == IndentAction.TALK_TO_HUMAN:
        await handle_play(
            call=call,
            client=client,
            context=Context.CONNECT_AGENT,
            text=TTSPrompt.END_CALL_TO_CONNECT_AGENT,
        )

    elif chat_res.intent == IndentAction.END_CALL:
        await handle_play(
            call=call,
            client=client,
            context=Context.GOODBYE,
            text=TTSPrompt.GOODBYE,
        )

    elif chat_res.intent in (
        IndentAction.NEW_CLAIM,
        IndentAction.UPDATED_CLAIM,
        IndentAction.NEW_OR_UPDATED_REMINDER,
    ):
        # Save in DB allowing demos to be more "real-time"
        await db.call_aset(call)
        # Answer with intermediate response
        await handle_play(
            call=call,
            client=client,
            store=False,
            text=chat_res.content,
        )
        # Recursively call intelligence to continue the conversation
        await intelligence(call, client)

    else:
        await handle_recognize_text(
            call=call,
            client=client,
            store=False,
            text=chat_res.content,
        )


async def handle_play(
    client: CallConnectionClient,
    call: CallModel,
    text: str,
    context: Optional[str] = None,
    store: bool = True,
) -> None:
    """
    Play a text to a call participant.

    If store is True, the text will be stored in the call messages. Compatible with text larger than 400 characters, in that case the text will be split in chunks and played sequentially.

    See: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts
    """
    if store:
        call.messages.append(
            CallMessageModel(content=text, persona=CallPersona.ASSISTANT)
        )

    # Split text in chunks of max 400 characters, separated by a comma
    chunks = []
    chunk = ""
    for word in text.split("."):  # Split by sentence
        to_add = f"{word}."
        if len(chunk) + len(to_add) >= 400:
            chunks.append(chunk)
            chunk = ""
        chunk += to_add
    if chunk:
        chunks.append(chunk)

    try:
        for chunk in chunks:
            _logger.debug(f"Playing chunk ({call.call_id}): {chunk}")
            client.play_media(
                operation_context=context,
                play_source=audio_from_text(chunk),
            )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before playing ({call.call_id})")


async def gpt_completion(system: LLMPrompt, call: CallModel) -> str:
    _logger.debug(f"Running GPT completion ({call.call_id})")

    messages = [
        {
            "content": LLMPrompt.DEFAULT_SYSTEM.format(
                date=datetime.now().strftime("%A %d %B %Y %H:%M:%S"),
                phone_number=call.phone_number,
            ),
            "role": "system",
        },
        {
            "content": system.format(
                claim=call.claim.model_dump_json(),
                conversation=json.dumps(call.messages, default=pydantic_encoder),
                reminders=json.dumps(call.reminders, default=pydantic_encoder),
            ),
            "role": "system",
        },
    ]
    _logger.debug(f"Messages: {messages}")

    content = None
    try:
        res = await oai_gpt.chat.completions.create(
            max_tokens=1000,  # Arbitrary limit
            messages=messages,
            model=CONFIG.openai.gpt_model,
            temperature=0,  # Most focused and deterministic
        )
        content = res.choices[0].message.content

    except Exception:
        _logger.warn(f"OpenAI API call error", exc_info=True)

    return content or ""


async def gpt_chat(call: CallModel) -> ActionModel:
    _logger.debug(f"Running GPT chat ({call.call_id})")

    messages = [
        {
            "content": LLMPrompt.DEFAULT_SYSTEM.format(
                date=datetime.now().strftime("%A %d %B %Y %H:%M:%S"),
                phone_number=call.phone_number,
            ),
            "role": "system",
        },
        {
            "content": LLMPrompt.CHAT_SYSTEM.format(
                claim=call.claim.model_dump_json(),
                reminders=json.dumps(call.reminders, default=pydantic_encoder),
            ),
            "role": "system",
        },
    ]
    for message in call.messages:
        if message.persona == CallPersona.HUMAN:
            messages.append(
                {
                    "content": message.content,
                    "role": "user",
                }
            )
        elif message.persona == CallPersona.ASSISTANT:
            if not message.tool_calls:
                messages.append(
                    {
                        "content": message.content,
                        "role": "assistant",
                    }
                )
            else:
                messages.append(
                    {
                        "content": message.content,
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": tool_call.tool_id,
                                "type": "function",
                                "function": {
                                    "arguments": tool_call.function_arguments,
                                    "name": tool_call.function_name,
                                },
                            }
                            for tool_call in message.tool_calls
                        ],
                    }
                )
                for tool_call in message.tool_calls:
                    messages.append(
                        {
                            "content": message.content,
                            "role": "tool",
                            "tool_call_id": tool_call.tool_id,
                        }
                    )
    _logger.debug(f"Messages: {messages}")

    customer_response_prop = "customer_response"
    tools = [
        {
            "type": "function",
            "function": {
                "description": "Use this if the user wants to talk to a human and Assistant is unable to help. This will transfer the customer to an human agent. Approval from the customer must be explicitely given. Example: 'I want to talk to a human', 'I want to talk to a real person'.",
                "name": IndentAction.TALK_TO_HUMAN,
                "parameters": {
                    "properties": {},
                    "required": [],
                    "type": "object",
                },
            },
        },
        {
            "type": "function",
            "function": {
                "description": "Use this if the user wants to end the call, or if the user is satisfied with the answer and confirmed the end of the call.",
                "name": IndentAction.END_CALL,
                "parameters": {
                    "properties": {},
                    "required": [],
                    "type": "object",
                },
            },
        },
        {
            "type": "function",
            "function": {
                "description": "Use this if the user wants to create a new claim. This will reset the claim and reminder data. Old is stored but not accessible anymore. Approval from the customer must be explicitely given. Example: 'I want to create a new claim'.",
                "name": IndentAction.NEW_CLAIM,
                "parameters": {
                    "properties": {
                        f"{customer_response_prop}": {
                            "description": "The text to be read to the customer to confirm the update. Only speak about this action. Use an imperative sentence. Example: 'I am updating the involved parties to Marie-Jeanne and Jean-Pierre', 'I am updating the policyholder contact info to 123 rue de la paix 75000 Paris, +33735119775, only call after 6pm'.",
                            "type": "string",
                        }
                    },
                    "required": [
                        customer_response_prop,
                    ],
                    "type": "object",
                },
            },
        },
        {
            "type": "function",
            "function": {
                "description": "Use this if the user wants to update a claim field with a new value. Example: 'Update claim explanation to: I was driving on the highway when a car hit me from behind', 'Update policyholder contact info to: 123 rue de la paix 75000 Paris, +33735119775, only call after 6pm'.",
                "name": IndentAction.UPDATED_CLAIM,
                "parameters": {
                    "properties": {
                        "field": {
                            "description": "The claim field to update.",
                            "enum": list(ClaimModel.editable_fields()),
                            "type": "string",
                        },
                        "value": {
                            "description": "The claim field value to update.",
                            "type": "string",
                        },
                        f"{customer_response_prop}": {
                            "description": "The text to be read to the customer to confirm the update. Only speak about this action. Use an imperative sentence. Example: 'I am updating the involved parties to Marie-Jeanne and Jean-Pierre', 'I am updating the policyholder contact info to 123 rue de la paix 75000 Paris, +33735119775, only call after 6pm'.",
                            "type": "string",
                        },
                    },
                    "required": [
                        customer_response_prop,
                        "field",
                        "value",
                    ],
                    "type": "object",
                },
            },
        },
        {
            "type": "function",
            "function": {
                "description": "Use this if you think there is something important to do in the future, and you want to be reminded about it. If it already exists, it will be updated with the new values. Example: 'Remind Assitant thuesday at 10am to call back the customer', 'Remind Assitant next week to send the report', 'Remind the customer next week to send the documents by the end of the month'.",
                "name": IndentAction.NEW_OR_UPDATED_REMINDER,
                "parameters": {
                    "properties": {
                        "description": {
                            "description": "Contextual description of the reminder. Should be detailed enough to be understood by anyone. Example: 'Watch model is Rolex Submariner 116610LN', 'User said the witnesses car was red but the police report says it was blue. Double check with the involved parties'.",
                            "type": "string",
                        },
                        "due_date_time": {
                            "description": "Datetime when the reminder should be triggered. Should be in the future, in the ISO format.",
                            "type": "string",
                        },
                        "title": {
                            "description": "Short title of the reminder. Should be short and concise, in the format 'Verb + Subject'. Title is unique and allows the reminder to be updated. Example: 'Call back customer', 'Send analysis report', 'Study replacement estimates for the stolen watch'.",
                            "type": "string",
                        },
                        "owner": {
                            "description": "The owner of the reminder. Can be 'customer', 'assistant', or a third party from the claim. Try to be as specific as possible, with a name. Example: 'customer', 'assistant', 'policyholder', 'witness', 'police'.",
                            "type": "string",
                        },
                        f"{customer_response_prop}": {
                            "description": "The text to be read to the customer to confirm the reminder. Only speak about this action. Use an imperative sentence. Example: 'I am creating a reminder for next week to call back the customer', 'I am creating a reminder for next week to send the report'.",
                            "type": "string",
                        },
                    },
                    "required": [
                        customer_response_prop,
                        "description",
                        "due_date_time",
                        "title",
                        "owner",
                    ],
                    "type": "object",
                },
            },
        },
    ]
    _logger.debug(f"Tools: {tools}")

    try:
        # TODO: Manage to catch timeouts to limit waiting time for end users
        res = await oai_gpt.chat.completions.create(
            max_tokens=400,  # Communication Services limit is 400 characters for TTS, 400 tokens ~= 300 words
            messages=messages,
            model=CONFIG.openai.gpt_model,
            temperature=0,  # Most focused and deterministic
            tools=tools,
        )

        content = res.choices[0].message.content or ""
        tool_calls = res.choices[0].message.tool_calls

        _logger.debug(f"Chat response: {content}")
        _logger.debug(f"Tool calls: {tool_calls}")

        intent = IndentAction.CONTINUE
        models = []
        if tool_calls:
            # TODO: Catch tool error individually
            for tool_call in tool_calls:
                name = tool_call.function.name
                arguments = tool_call.function.arguments
                _logger.info(f"Tool call {name} with parameters {arguments}")

                model = CallToolModel(
                    content="",
                    function_arguments=arguments,
                    function_name=name,
                    tool_id=tool_call.id,
                )

                if name == IndentAction.TALK_TO_HUMAN:
                    intent = IndentAction.TALK_TO_HUMAN

                elif name == IndentAction.END_CALL:
                    intent = IndentAction.END_CALL

                elif name == IndentAction.UPDATED_CLAIM:
                    intent = IndentAction.UPDATED_CLAIM
                    parameters = json.loads(arguments)

                    if not customer_response_prop in parameters:
                        _logger.warn(
                            f"Missing {customer_response_prop} prop in {arguments}, please fix this!"
                        )
                    else:
                        content += parameters[customer_response_prop] + " "

                    setattr(call.claim, parameters["field"], parameters["value"])
                    model.content = f"Updated claim field \"{parameters['field']}\" with value \"{parameters['value']}\"."

                elif name == IndentAction.NEW_CLAIM:
                    intent = IndentAction.NEW_CLAIM
                    parameters = json.loads(arguments)

                    if not customer_response_prop in parameters:
                        _logger.warn(
                            f"Missing {customer_response_prop} prop in {arguments}, please fix this!"
                        )
                    else:
                        content += parameters[customer_response_prop] + " "

                    call.claim = ClaimModel()
                    call.reminders = []
                    model.content = "Claim and reminders created reset."

                elif name == IndentAction.NEW_OR_UPDATED_REMINDER:
                    intent = IndentAction.NEW_OR_UPDATED_REMINDER
                    parameters = json.loads(arguments)

                    if not customer_response_prop in parameters:
                        _logger.warn(
                            f"Missing {customer_response_prop} prop in {arguments}, please fix this!"
                        )
                    else:
                        content += parameters[customer_response_prop] + " "

                    updated = False
                    for reminder in call.reminders:
                        if reminder.title == parameters["title"]:
                            reminder.description = parameters["description"]
                            reminder.due_date_time = parameters["due_date_time"]
                            reminder.owner = parameters["owner"]
                            model.content = (
                                f"Reminder \"{parameters['title']}\" updated."
                            )
                            updated = True
                            break

                    if not updated:
                        call.reminders.append(
                            ReminderModel(
                                description=parameters["description"],
                                due_date_time=parameters["due_date_time"],
                                title=parameters["title"],
                                owner=parameters["owner"],
                            )
                        )
                        model.content = f"Reminder \"{parameters['title']}\" created."

                models.append(model)

        call.messages.append(
            CallMessageModel(
                content=content,
                persona=CallPersona.ASSISTANT,
                tool_calls=models,
            )
        )

        return ActionModel(
            content=content,
            intent=intent,
        )

    except Exception:
        _logger.warn(f"OpenAI API call error", exc_info=True)

    return ActionModel(content=TTSPrompt.ERROR, intent=IndentAction.CONTINUE)


async def handle_recognize_text(
    client: CallConnectionClient,
    call: CallModel,
    text: str,
    store: bool = True,
) -> None:
    """
    Play a text to a call participant and start recognizing the response.

    If store is True, the text will be stored in the call messages. Starts by playing text, then the "ready" sound, and finally starts recognizing the response.
    """
    await handle_play(
        call=call,
        client=client,
        store=store,
        text=text,
    )

    _logger.debug(f"Recognizing ({call.call_id})")
    await handle_recognize_media(
        call=call,
        client=client,
        sound=SoundPrompt.READY,
    )


async def handle_recognize_media(
    client: CallConnectionClient,
    call: CallModel,
    sound: SoundPrompt,
) -> None:
    """
    Play a media to a call participant and start recognizing the response.

    TODO: Disable or lower profanity filter. The filter seems enabled by default, it replaces words like "holes in my roof" by "*** in my roof". This is not acceptable for a call center.
    """
    try:
        client.start_recognizing_media(
            end_silence_timeout=3,  # Sometimes user includes breaks in their speech
            input_type=RecognizeInputType.SPEECH,
            play_prompt=FileSource(url=sound),
            speech_language=CONFIG.workflow.conversation_lang,
            target_participant=PhoneNumberIdentifier(call.phone_number),
        )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before recognizing ({call.call_id})")


async def handle_media(
    client: CallConnectionClient,
    call: CallModel,
    sound: SoundPrompt,
    context: Optional[str] = None,
) -> None:
    try:
        client.play_media(
            operation_context=context,
            play_source=FileSource(url=sound),
        )
    except ResourceNotFoundError:
        _logger.debug(f"Call hung up before playing ({call.call_id})")


async def handle_hangup(client: CallConnectionClient, call: CallModel) -> None:
    _logger.debug(f"Hanging up call ({call.call_id})")
    try:
        client.hang_up(is_for_everyone=True)
    except ResourceNotFoundError:
        _logger.debug(f"Call already hung up ({call.call_id})")

    call.messages.append(
        CallMessageModel(content="Customer ended the call.", persona=CallPersona.HUMAN)
    )

    content = await gpt_completion(LLMPrompt.SMS_SUMMARY_SYSTEM, call)
    _logger.info(f"SMS report ({call.call_id}): {content}")

    try:
        responses = sms_client.send(
            from_=str(CONFIG.communication_service.phone_number),
            message=content,
            to=call.phone_number,
        )
        response = responses[0]

        if response.successful:
            _logger.info(
                f"SMS report sent {response.message_id} to {response.to} ({call.call_id})"
            )
            call.messages.append(
                CallMessageModel(
                    content=f"SMS report sent to {response.to}: {content}",
                    persona=CallPersona.ASSISTANT,
                )
            )
        else:
            _logger.warn(
                f"Failed SMS to {response.to}, status {response.http_status_code}, error {response.error_message} ({call.call_id})"
            )
            call.messages.append(
                CallMessageModel(
                    content=f"Failed to send SMS report to {response.to}: {response.error_message}",
                    persona=CallPersona.ASSISTANT,
                )
            )

    except Exception:
        _logger.warn(
            f"Failed SMS to {call.phone_number} ({call.call_id})", exc_info=True
        )


def audio_from_text(text: str) -> SsmlSource:
    """
    Generate an audio source that can be read by Azure Communication Services SDK.

    Text requires to be SVG escaped, and SSML tags are used to control the voice. Plus, text is slowed down by 5% to make it more understandable for elderly people. Text is also truncated to 400 characters, as this is the limit of Azure Communication Services TTS, but a warning is logged.
    """
    # Azure Speech Service TTS limit is 400 characters
    if len(text) > 400:
        _logger.warning(
            f"Text is too long to be processed by TTS, truncating to 400 characters, fix this!"
        )
        text = text[:400]
    ssml = f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="{CONFIG.workflow.conversation_lang}"><voice name="{CONFIG.communication_service.voice_name}" effect="eq_telecomhp8k"><lexicon uri="{CONFIG.resources.public_url}/lexicon.xml"/><prosody rate="0.95">{text}</prosody></voice></speak>'
    return SsmlSource(ssml_text=ssml)


async def callback_url(caller_id: str) -> str:
    """
    Generate the callback URL for a call.

    If the caller has already called, use the same call ID, to keep the conversation history. Otherwise, create a new call ID.
    """
    call = await db.call_asearch_one(caller_id)
    if not call:
        call = CallModel(phone_number=caller_id)
        await db.call_aset(call)
    return f"{CALL_EVENT_URL}/{call.call_id}"
