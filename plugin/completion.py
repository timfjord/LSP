from .core.edit import parse_text_edit
from .core.logging import debug
from .core.promise import Promise
from .core.protocol import CompletionItem
from .core.protocol import CompletionItemKind
from .core.protocol import CompletionList
from .core.protocol import CompletionParams
from .core.protocol import Error
from .core.protocol import InsertReplaceEdit
from .core.protocol import InsertTextFormat
from .core.protocol import MarkupContent, MarkedString, MarkupKind
from .core.protocol import Range
from .core.protocol import Request
from .core.protocol import TextEdit
from .core.registry import LspTextCommand
from .core.sessions import Session
from .core.settings import userprefs
from .core.typing import Callable, List, Dict, Optional, Generator, Tuple, Union, cast
from .core.views import format_completion
from .core.views import FORMAT_STRING, FORMAT_MARKUP_CONTENT
from .core.views import MarkdownLangMap
from .core.views import minihtml
from .core.views import range_to_region
from .core.views import show_lsp_popup
from .core.views import text_document_position_params
from .core.views import update_lsp_popup
import functools
import sublime
import weakref
import webbrowser

SessionName = str
CompletionResponse = Union[List[CompletionItem], CompletionList, None]
ResolvedCompletions = Tuple[Union[CompletionResponse, Error], 'weakref.ref[Session]']


def get_text_edit_range(text_edit: Union[TextEdit, InsertReplaceEdit]) -> Range:
    if 'insert' in text_edit and 'replace' in text_edit:
        text_edit = cast(InsertReplaceEdit, text_edit)
        insert_mode = userprefs().completion_insert_mode
        if LspCommitCompletionWithOppositeInsertMode.active:
            insert_mode = 'replace' if insert_mode == 'insert' else 'insert'
        return text_edit.get(insert_mode)  # type: ignore
    text_edit = cast(TextEdit, text_edit)
    return text_edit['range']


class QueryCompletionsTask:
    """
    Represents pending completion requests.

    Can be canceled while in progress in which case the "on_done_async" callback will get immediately called with empty
    list and the pending response from the server(s) will be canceled and results ignored.

    All public methods must only be called on the async thread and the "on_done_async" callback will also be called
    on the async thread.
    """
    def __init__(
        self,
        view: sublime.View,
        location: int,
        triggered_manually: bool,
        on_done_async: Callable[[List[sublime.CompletionItem], int], None]
    ) -> None:
        self._view = view
        self._location = location
        self._triggered_manually = triggered_manually
        self._on_done_async = on_done_async
        self._resolved = False
        self._pending_completion_requests = {}  # type: Dict[int, weakref.ref[Session]]

    def query_completions_async(self, sessions: List[Session]) -> None:
        promises = [self._create_completion_request_async(session) for session in sessions]
        Promise.all(promises).then(lambda response: self._resolve_completions_async(response))

    def _create_completion_request_async(self, session: Session) -> Promise[ResolvedCompletions]:
        params = cast(CompletionParams, text_document_position_params(self._view, self._location))
        request = Request.complete(params, self._view)
        promise, request_id = session.send_request_task_2(request)
        weak_session = weakref.ref(session)
        self._pending_completion_requests[request_id] = weak_session
        return promise.then(lambda response: self._on_completion_response_async(response, request_id, weak_session))

    def _on_completion_response_async(
        self, response: CompletionResponse, request_id: int, weak_session: 'weakref.ref[Session]'
    ) -> ResolvedCompletions:
        self._pending_completion_requests.pop(request_id, None)
        return (response, weak_session)

    def _resolve_completions_async(self, responses: List[ResolvedCompletions]) -> None:
        if self._resolved:
            return
        LspResolveDocsCommand.completions = {}
        items = []  # type: List[sublime.CompletionItem]
        errors = []  # type: List[Error]
        flags = 0  # int
        prefs = userprefs()
        if prefs.inhibit_snippet_completions:
            flags |= sublime.INHIBIT_EXPLICIT_COMPLETIONS
        if prefs.inhibit_word_completions:
            flags |= sublime.INHIBIT_WORD_COMPLETIONS
        view_settings = self._view.settings()
        include_snippets = view_settings.get("auto_complete_include_snippets") and \
            (self._triggered_manually or view_settings.get("auto_complete_include_snippets_when_typing"))
        for response, weak_session in responses:
            if isinstance(response, Error):
                errors.append(response)
                continue
            session = weak_session()
            if not session:
                continue
            response_items = []  # type: List[CompletionItem]
            if isinstance(response, dict):
                response_items = response["items"] or []
                if response.get("isIncomplete", False):
                    flags |= sublime.DYNAMIC_COMPLETIONS
            elif isinstance(response, list):
                response_items = response
            response_items = sorted(response_items, key=lambda item: item.get("sortText") or item["label"])
            LspResolveDocsCommand.completions[session.config.name] = response_items
            can_resolve_completion_items = session.has_capability('completionProvider.resolveProvider')
            config_name = session.config.name
            items.extend(
                format_completion(response_item, index, can_resolve_completion_items, config_name, self._view.id())
                for index, response_item in enumerate(response_items)
                if include_snippets or response_item.get("kind") != CompletionItemKind.Snippet)
        if items:
            flags |= sublime.INHIBIT_REORDER
        if errors:
            error_messages = ", ".join(str(error) for error in errors)
            sublime.status_message('Completion error: {}'.format(error_messages))
        self._resolve_task_async(items, flags)

    def cancel_async(self) -> None:
        self._resolve_task_async([])
        self._cancel_pending_requests_async()

    def _cancel_pending_requests_async(self) -> None:
        for request_id, weak_session in self._pending_completion_requests.items():
            session = weak_session()
            if session:
                session.cancel_request(request_id, False)
        self._pending_completion_requests.clear()

    def _resolve_task_async(self, completions: List[sublime.CompletionItem], flags: int = 0) -> None:
        if not self._resolved:
            self._resolved = True
            self._on_done_async(completions, flags)


class LspResolveDocsCommand(LspTextCommand):

    completions = {}  # type: Dict[SessionName, List[CompletionItem]]

    def run(self, edit: sublime.Edit, index: int, session_name: str, event: Optional[dict] = None) -> None:

        def run_async() -> None:
            item = self.completions[session_name][index]
            session = self.session_by_name(session_name, 'completionProvider.resolveProvider')
            if session:
                request = Request.resolveCompletionItem(item, self.view)
                language_map = session.markdown_language_id_to_st_syntax_map()
                handler = functools.partial(self._handle_resolve_response_async, language_map)
                session.send_request_async(request, handler)
            else:
                self._handle_resolve_response_async(None, item)

        sublime.set_timeout_async(run_async)

    def _handle_resolve_response_async(self, language_map: Optional[MarkdownLangMap], item: CompletionItem) -> None:
        detail = ""
        documentation = ""
        if item:
            detail = self._format_documentation(item.get('detail') or "", language_map)
            documentation = self._format_documentation(item.get("documentation") or "", language_map)
        if not documentation:
            markdown = {"kind": MarkupKind.Markdown, "value": "*No documentation available.*"}  # type: MarkupContent
            # No need for a language map here
            documentation = self._format_documentation(markdown, None)
        minihtml_content = ""
        if detail:
            minihtml_content += "<div class='highlight'>{}</div>".format(detail)
        if documentation:
            minihtml_content += documentation

        def run_main() -> None:
            if not self.view.is_valid():
                return
            if self.view.is_popup_visible():
                update_lsp_popup(self.view, minihtml_content, md=False)
            else:
                show_lsp_popup(
                    self.view,
                    minihtml_content,
                    flags=sublime.COOPERATE_WITH_AUTO_COMPLETE,
                    md=False,
                    on_navigate=self._on_navigate)

        sublime.set_timeout(run_main)

    def _format_documentation(
        self,
        content: Union[MarkedString, MarkupContent],
        language_map: Optional[MarkdownLangMap]
    ) -> str:
        return minihtml(self.view, content, FORMAT_STRING | FORMAT_MARKUP_CONTENT, language_map)

    def _on_navigate(self, url: str) -> None:
        webbrowser.open(url)


class LspCommitCompletionWithOppositeInsertMode(LspTextCommand):
    active = False

    def run(self, edit: sublime.Edit, event: Optional[dict] = None) -> None:
        LspCommitCompletionWithOppositeInsertMode.active = True
        self.view.run_command("commit_completion")
        LspCommitCompletionWithOppositeInsertMode.active = False


class LspSelectCompletionItemCommand(LspTextCommand):
    def run(self, edit: sublime.Edit, item: CompletionItem, session_name: str) -> None:
        text_edit = item.get("textEdit")
        if text_edit:
            new_text = text_edit["newText"].replace("\r", "")
            edit_region = range_to_region(get_text_edit_range(text_edit), self.view)
            for region in self._translated_regions(edit_region):
                self.view.erase(edit, region)
        else:
            new_text = item.get("insertText") or item["label"]
            new_text = new_text.replace("\r", "")
        if item.get("insertTextFormat", InsertTextFormat.PlainText) == InsertTextFormat.Snippet:
            self.view.run_command("insert_snippet", {"contents": new_text})
        else:
            self.view.run_command("insert", {"characters": new_text})
        # todo: this should all run from the worker thread
        session = self.session_by_name(session_name, 'completionProvider.resolveProvider')
        additional_text_edits = item.get('additionalTextEdits')
        if session and not additional_text_edits:
            session.send_request_async(
                Request.resolveCompletionItem(item, self.view),
                functools.partial(self._on_resolved_async, session_name))
        else:
            self._on_resolved(session_name, item)

    def _on_resolved_async(self, session_name: str, item: CompletionItem) -> None:
        sublime.set_timeout(functools.partial(self._on_resolved, session_name, item))

    def _on_resolved(self, session_name: str, item: CompletionItem) -> None:
        additional_edits = item.get('additionalTextEdits')
        if additional_edits:
            edits = [parse_text_edit(additional_edit) for additional_edit in additional_edits]
            self.view.run_command("lsp_apply_document_edit", {'changes': edits})
        command = item.get("command")
        if command:
            debug('Running server command "{}" for view {}'.format(command, self.view.id()))
            args = {
                "command_name": command["command"],
                "command_args": command.get("arguments"),
                "session_name": session_name
            }
            self.view.run_command("lsp_execute", args)

    def _translated_regions(self, edit_region: sublime.Region) -> Generator[sublime.Region, None, None]:
        selection = self.view.sel()
        primary_cursor_position = selection[0].b
        for region in reversed(selection):
            # For each selection region, apply the same removal as for the "primary" region.
            # To do that, translate, or offset, the LSP edit region into the non-"primary" regions.
            # The concept of "primary" is our own, and there is no mention of it in the LSP spec.
            translation = region.b - primary_cursor_position
            translated_edit_region = sublime.Region(edit_region.a + translation, edit_region.b + translation)
            yield translated_edit_region
