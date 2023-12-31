from operator import itemgetter
from typing import Any, AsyncIterator, Dict, List, Union

from langchain.agents import AgentExecutor
from langchain.agents.openai_functions_agent.base import create_openai_functions_agent
from langchain.memory import ConversationSummaryBufferMemory
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools.google_search.tool import GoogleSearchRun
from langchain.tools.wikipedia.tool import WikipediaQueryRun
from langchain.utilities.google_search import GoogleSearchAPIWrapper
from langchain.utilities.wikipedia import WikipediaAPIWrapper
from langchain_core.language_models.llms import BaseLanguageModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_mistralai.chat_models import ChatMistralAI
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from libs.config import Settings
from libs.models import AzureDALLELLM, ChatGoogleGenerativeAIWithoutSafety
from libs.tools import (
    AzureDallERun,
    OpenWeatherMapQueryRunEnhanced,
    TwitterTranslatorRun,
)


def text_model_from_config(config: Settings) -> BaseLanguageModel:
    if config.is_openai:
        return ChatOpenAI(
            model=config.openai_model_name,
            temperature=config.temperature,
        )

    if config.is_azure:
        return AzureChatOpenAI(
            azure_deployment=config.azure_openai_deployment,
            api_version=config.azure_openai_api_version,
            temperature=config.temperature,
        )

    if config.is_mistral:
        return ChatMistralAI(temperature=config.temperature, model=config.mistral_model)  # type: ignore

    if config.is_google:
        return ChatGoogleGenerativeAIWithoutSafety(model="gemini-pro", temperature=config.temperature, convert_system_message_to_human=True)  # type: ignore

    raise ValueError("Only Azure and Google models are supported at this time")


def vison_model_from_config(config: Settings) -> BaseLanguageModel | None:
    if config.has_vision:
        return ChatGoogleGenerativeAIWithoutSafety(model="gemini-pro-vision", temperature=config.temperature)  # type: ignore

    return None


def dalle_model_from_config(config: Settings) -> BaseLanguageModel | None:
    if config.has_dalle:
        return AzureDALLELLM(
            api_version=config.azure_dalle_api_version,
            api_key=config.azure_dalle_api_key,
            azure_endpoint=config.azure_dalle_endpoint or "",
            azure_deployment=config.azure_dalle_deployment,
        )

    return None


class LLMAgentExecutor:
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", "You are a helpful AI assistant."),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{input}"),
        ]
    )
    history = dict[str, ConversationSummaryBufferMemory]()

    def __init__(self, config: Settings):
        self.text_model = text_model_from_config(config=config)
        self.vision_model = vison_model_from_config(config=config)
        self.dalle_model = dalle_model_from_config(config=config)
        self.config = config
        self.history_max_size = config.history_max_size

    def get_history(self, user: str) -> ConversationSummaryBufferMemory:
        m = self.history.get(
            user,
            ConversationSummaryBufferMemory(
                llm=self.text_model,
                return_messages=True,
                max_token_limit=self.history_max_size,
                memory_key="history",
                output_key="output",  # for hacky reasons
            ),
        )
        self.history[user] = m
        return m

    def clear_history(self, user: str):
        if user in self.history:
            self.history[user].clear()

    def save_history(self, user: str, input: str, response: str):
        self.get_history(user).save_context({"input": input}, {"output": response})

    async def query(self, user: str, message: Union[str, List[Union[str, Dict]]]) -> AsyncIterator[str]:
        if isinstance(message, list):
            if self.vision_model:
                msg = HumanMessage(content=message)
                async for s in self.vision_model.astream([msg]):
                    yield s.content
                return
            raise ValueError("Vision model is not enabled")

        memory = self.get_history(user)

        tools: List[Any] = []
        if self.config.enable_google_search:
            tools.append(GoogleSearchRun(api_wrapper=GoogleSearchAPIWrapper()))  # type: ignore
        if self.config.enable_wikipedia:
            tools.append(WikipediaQueryRun(api_wrapper=WikipediaAPIWrapper()))  # type: ignore
        if self.config.openweathermap_api_key:
            tools.append(OpenWeatherMapQueryRunEnhanced())
        if self.dalle_model:
            tools.append(AzureDallERun(client=self.dalle_model))
        if self.config.enable_twitter_translator:
            tools.append(TwitterTranslatorRun())

        if (self.config.is_openai or self.config.is_azure) and len(tools) > 0:
            self.prompt.append(MessagesPlaceholder(variable_name="agent_scratchpad"))
            # self.prompt.append can't update input_variables, so need this
            self.prompt.input_variables.append("agent_scratchpad")
            agent = create_openai_functions_agent(self.text_model, tools, self.prompt)
            agent_executor = AgentExecutor(agent=agent, tools=tools, verbose=True)  # type: ignore
            async for v in agent_executor.astream(
                {"input": message, "history": memory.load_memory_variables({})[memory.memory_key]}
            ):
                if isinstance(v, dict) and "output" in v:
                    yield v["output"]  # type: ignore
            return

        chain = (
            RunnablePassthrough.assign(history=RunnableLambda(memory.load_memory_variables) | itemgetter("history"))
            | self.prompt
            | self.text_model
        )
        async for c in chain.astream({"input": message}):
            yield c.content
