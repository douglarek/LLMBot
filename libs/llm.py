from operator import itemgetter
from typing import Any, AsyncIterator, Dict, List, Union

from langchain.chat_models import AzureChatOpenAI
from langchain.memory import ConversationSummaryBufferMemory
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.tools.google_search.tool import GoogleSearchRun
from langchain.tools.openweathermap.tool import OpenWeatherMapQueryRun
from langchain.tools.wikipedia.tool import WikipediaQueryRun
from langchain.utilities.google_search import GoogleSearchAPIWrapper
from langchain.utilities.wikipedia import WikipediaAPIWrapper
from langchain_core.language_models.llms import BaseLanguageModel
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_google_genai import ChatGoogleGenerativeAI

from libs.config import Settings
from libs.models import AzureDALLELLM
from libs.tools import DALLEQueryRun


def text_model_from_config(config: Settings) -> BaseLanguageModel:
    if config.is_azure:
        return AzureChatOpenAI(
            azure_deployment=config.azure_openai_deployment,
            api_version=config.azure_openai_api_version,
            temperature=config.temperature,
            streaming=True,
        )

    if config.is_google:
        return ChatGoogleGenerativeAI(model="gemini-pro", temperature=config.temperature, convert_system_message_to_human=True)  # type: ignore

    raise ValueError("Only Azure and Google models are supported at this time")


def vison_model_from_config(config: Settings) -> BaseLanguageModel | None:
    if config.has_vision:
        return ChatGoogleGenerativeAI(model="gemini-pro-vision", temperature=config.temperature)  # type: ignore

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
            ("system", "You are a helpful chatbot"),
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
                for s in self.vision_model.stream([msg]):
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
            tools.append(OpenWeatherMapQueryRun())
        if self.dalle_model:
            tools.append(DALLEQueryRun(client=self.dalle_model))

        chain = (
            RunnablePassthrough.assign(history=RunnableLambda(memory.load_memory_variables) | itemgetter("history"))
            | self.prompt
            | self.text_model
        )
        for c in chain.stream({"input": message}):
            yield c.content
        return
