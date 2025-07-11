import os
import re
import math
import json
import time
import asyncio
from typing import Annotated
from typing_extensions import TypedDict
from langchain_core.tools import tool
from langchain.agents import Tool
from langchain_core.messages import ToolMessage, ToolCall, AIMessageChunk, AIMessage
from langgraph.graph import StateGraph, START
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langchain.chat_models import init_chat_model
from pydantic import BaseModel
from dotenv import load_dotenv
import psycopg2
from psycopg2 import pool

from utils.render import render 
from tools.tools import define_tools


load_dotenv()


llm = init_chat_model(
    model="gpt-4-turbo",
    temperature=0,
    api_key=os.environ["OPENAI_API_KEY"],
    streaming=True
    )


# System message to guide the LLM
SYSTEM_MESSAGE = """You are a helpful AI data analyst.

When given a user question:
0. Start by explaining your analysis approach in exactly 1 sentence (how you plan to break down and answer the question). Do not mention SQL or technical details.
1. Generate exactly 3 analytical sub-questions that would help answer the main question using the generate_analytical_questions tool.
2. Process each sub-question one at a time (in series, not parallel):
    a. Generate a SQL query to answer it using the text_to_sql tool.
    b. Execute the SQL query using the run_sql tool.
    c. Analyze the returned data using the analyse_data tool.
    d. Move to the next sub-question only after completing the previous one.
3. Summarize your findings in clear markdown, using bullet points and code blocks for data.

Rules:
- Use only the provided schema.
- Always show your reasoning and steps.
- Never use table formatting (|), use bullet points or code blocks for structured data.
- Add LIMIT 50 to SQL queries if not present.
- Use the analyse_data tool to get insights from SQL results before summarizing.
- Process exactly 3 questions, one at a time.
- Always end each response with a newline or double newline for proper formatting.
"""

# --- Define Graph State ----
class State(TypedDict):
    messages: Annotated[list, add_messages]


# TODO: find a way to remove this function - probs resolve the extending function in the streaming loop
def aggregate_tool_calls(tool_calls_accumulator):
    grouped = {}

    for call in tool_calls_accumulator:
        idx = call.get("index")
        if idx is None:
            continue

        if idx not in grouped:
            grouped[idx] = {
                "index": idx,
                "id": call.get("id"),
                "name": call.get("function", {}).get("name"),
                "arguments": call.get("function", {}).get("arguments", ""),
                "type": call.get("type"),
            }
        else:
            grouped[idx]["arguments"] += call.get("function", {}).get("arguments", "")

        # Prefer non-None values if found in later chunks
        if not grouped[idx]["id"] and call.get("id"):
            grouped[idx]["id"] = call["id"]
        if not grouped[idx]["name"] and call.get("function", {}).get("name"):
            grouped[idx]["name"] = call["function"]["name"]
        if not grouped[idx]["type"] and call.get("type"):
            grouped[idx]["type"] = call["type"]

    # Convert back to expected format
    aggregated_calls = []
    for val in grouped.values():
        aggregated_calls.append({
            "index": val["index"],
            "id": val["id"],
            "function": {
                "name": val["name"],
                "arguments": val["arguments"]
            },
            "type": val["type"]
        })

    return aggregated_calls



# --- LLM Node (detects tool usage) ---
def make_llm_node(llm_with_tools, token_stream_callback=None):
    async def call_llm(state: State):
        last_msg = state["messages"][-1]

        collected_chunks = []  # this appears to be unused
        content_accumulator = []
        tool_calls_accumulator = []

        # Streaming LLM output
        async for chunk in llm_with_tools.astream(state["messages"]):
            print(chunk)
            collected_chunks.append(chunk)

            # TODO: here and in tools check if chunk.content is not null
            if token_stream_callback:
                await token_stream_callback(chunk.content)
            if chunk.content:
                content_accumulator.append(chunk.content)
            if "tool_calls" in chunk.additional_kwargs:
                tool_calls_accumulator.extend(chunk.additional_kwargs["tool_calls"])
        
        # if token_stream_callback:
        #     await token_stream_callback("[DONE]")
        
        full_content = "".join(content_accumulator)

        aggregated = aggregate_tool_calls(tool_calls_accumulator)
        print("tool_calls_accumulator: ", tool_calls_accumulator)
        print("aggregated: ", aggregated)
        if aggregated:
            print("in aggregated")

            # TODO: cleanup openai_style_tool_calls and tool_calls

            # tool_calls = list(map(lambda call: ToolCall(
            #     id=call["id"],
            #     name=call["function"]["name"],
            #     args=json.loads(call["function"]["arguments"])
            # ), aggregated))

            openai_style_tool_calls = [
                {
                    "id": call["id"],
                    "type": call["type"],
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"]
                    }
                }
                for call in aggregated
            ]
            print("openai_style_tool_calls: ", openai_style_tool_calls)
            print("full_content: ", full_content)

            yield {
                "tool_calls": [
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        args=json.loads(call["function"]["arguments"]),
                    ) for call in aggregated
                ],
                "messages": [
                    AIMessage(
                        content=full_content,
                        additional_kwargs={"tool_calls": openai_style_tool_calls}
                    )
                ]
            }
            return
            # yield {"tool_calls": tool_calls, "messages": [AIMessage(content=full_content, additional_kwargs={"tool_calls": openai_style_tool_calls})]}
        # print(f"Yielding final chunks: ${collected_chunks}")
        yield {"messages": [AIMessage(content=full_content)]}
        
    return call_llm

def make_end_node(token_stream_callback=None):
    async def end_node(state: State) -> State:
        if token_stream_callback:
            await token_stream_callback("[DONE]")
        
        print("[end_node] Final state: ", state)
        return state
    
    return end_node


def custom_tools_condition(state: State) -> str:
    last_msg = state["messages"][-1]
    print("custom_tools_condition last_msg: ", last_msg)

    # If we have a tool message, we should continue to the LLM to process the result
    if isinstance(last_msg, ToolMessage):
        return "llm"
    
    # If we have an AI message with tool calls, execute them
    if isinstance(last_msg, AIMessage) and getattr(last_msg, "tool_calls", None):
        return "tools"
    
    # If we have a regular AI message without tool calls, we're done
    return 'end'

# --- Build Graph function ---
# TODO: type payload
def build_graph(payload, token_stream_callback=None):
    graph_builder = StateGraph(State)

    # Define tools and attach to llm
    tools = define_tools(payload, token_stream_callback)
    llm_with_tools = llm.bind_tools(tools)


    # --- LLM Node ---
    llm_node = make_llm_node(llm_with_tools, token_stream_callback)

    # --- Tool Executor Node ---
    tool_node = ToolNode(tools)

    # --- End Node ---
    end_node = make_end_node(token_stream_callback)

    graph_builder.add_node("llm", llm_node)
    graph_builder.add_node("tools", tool_node)
    graph_builder.add_node("end", end_node)

    graph_builder.set_entry_point("llm")

    graph_builder.add_conditional_edges("llm", custom_tools_condition)
    graph_builder.add_edge("tools", "llm")

    # --- Render Graph (optional) ---
    # render(graph)

    return graph_builder.compile()



async def stream_graph_updates(graph, user_input: str):
    print("Assistant: ", end="", flush=True)

    # Add system message to the initial state
    initial_state = {
        "messages": [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": user_input}
        ]
    }

    async for step in graph.astream(initial_state):
        for node_output in step.values():
            for msg in node_output.get("messages", []):
                content = getattr(msg, "content", None)
                if content:
                    print(content, end="", flush=True)

    print()


async def main():
    # Only import schema when running locally
    from config.database import transactions_schema
    # TODO: split main.py into a main and local file for running agent from cli

    payload = {
        "organization_id": "",  # TODO: this may need to be filled for local agent to run sql successfully
        "connection_id": "",  # TODO: this may need to be filled for local agent to run sql successfully
        'tenant_jwt': "",
        "schema": transactions_schema
    }

    graph = build_graph(payload=payload)
    while True:
        user_input = input("User: ")
        if user_input.lower() in ["quit", "exit", "q"]:
            print("exiting")
            break
        await stream_graph_updates(graph, user_input)


if __name__ == "__main__":
    asyncio.run(main())
