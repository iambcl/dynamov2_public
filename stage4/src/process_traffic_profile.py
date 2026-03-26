from typing import TypedDict, Annotated, Sequence, List
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from dynamov2.database.remote_db import RemoteDBHelper
from dynamov2.logger.logger import CustomLogger
import json, re, os
from langchain_openai import ChatOpenAI

'''
Determine application protocol based on traffic information processed
'''


# llm = 'gemini-2.5-flash'
# llm = 'gemini-2.5-flash-lite'
# llm = 'gpt-oss:20b'
# llm = 'qwen3-coder:480b'
# llm = 'gpt-4o-mini'
# llm = 'gpt-5-nano'
# llm = 'gpt-5-mini'
# llm = 'gpt-5'

def langchain_model(llm):
    if 'gemini' in llm:
        model = ChatGoogleGenerativeAI(model=llm,
                                    temperature=0,
                                    max_retries=3)
    elif 'gpt-5' in llm:
        model = ChatOpenAI(model=llm,
                    temperature=0,
                    max_retries=3)
    return model
    

def process_traffic_profile(traffic_profile: dict, distinct_applications: list) -> dict:
    prompt = PromptTemplate(
        input_variables=["application_flow", "distinct_applications"],
        template=(
            "You are given network traffic observations recorded over 60 seconds.\n\n"
            "Your task:\n"
            "1. Determine whether there is **application traffic** beyond initial download traffic or DNS.\n"
            "   - Download traffic typically appears at the beginning and does not persist.\n"
            "   - DNS traffic alone does NOT count as application traffic.\n"
            "2. If application traffic is present, identify suitable application protocols.\n\n"
            "Protocol selection rules:\n"
            "- Prefer selecting **protocols** from the following list if it fits:\n"
            "{distinct_applications}\n"
            "- Only choose protocol outside the list if none are suitable.\n"
            "- Do NOT explain the protocol name itself.\n\n"
            "Return a JSON object with exactly these keys:\n"
            "- application_traffic_present (boolean)\n"
            "- application (string or null)\n"
            "- reason (short explanation)\n\n"
            "Traffic observations:\n"
            "{application_flow}"
        )
    )
    llm = os.getenv("LLM")
    model = langchain_model(llm)    
    chain = prompt | model
    try:
        response = chain.invoke({"application_flow": traffic_profile, "distinct_applications": distinct_applications}).content
        cleaned = re.sub(r"```(?:json)?", "", response).strip()
        final_output = json.loads(cleaned)
        print(final_output)
        return final_output
    except json.decoder.JSONDecodeError as e:
        return None

if __name__ == '__main__':
    pass