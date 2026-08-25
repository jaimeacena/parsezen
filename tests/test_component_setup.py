from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QScrollArea

from parsezen.component_readiness import ComponentReadiness, ReadinessStatus
from parsezen.local_ai_policy import ComponentCapability
from parsezen.local_models import LocalHardware
from parsezen.presentation.component_setup import ComponentSetupDialog


def test_component_setup_renders_only_the_two_fixed_capabilities(qtbot) -> None:
    dialog = ComponentSetupDialog(
        states={
            ComponentCapability.TRANSLATION: ReadinessStatus.PREPARED,
            ComponentCapability.REVIEW: ReadinessStatus.INSUFFICIENT,
        }
    )
    qtbot.addWidget(dialog)
    dialog.show()

    assert set(dialog.cards) == {
        ComponentCapability.TRANSLATION,
        ComponentCapability.REVIEW,
    }
    assert dialog.cards[ComponentCapability.TRANSLATION].status_label.text() == "Preparado"
    assert dialog.cards[ComponentCapability.REVIEW].status_label.text() == "Equipo insuficiente"
    assert not dialog.findChildren(QScrollArea)
    assert "qwen" not in dialog.windowTitle().casefold()


def test_downloadable_card_emits_only_its_capability_and_does_not_download(qtbot) -> None:
    dialog = ComponentSetupDialog(
        readiness={
            ComponentCapability.TRANSLATION: ComponentReadiness(
                ComponentCapability.TRANSLATION,
                ReadinessStatus.DOWNLOADABLE,
            )
        }
    )
    qtbot.addWidget(dialog)
    dialog.show()
    requests: list[object] = []
    dialog.download_requested.connect(requests.append)

    button = dialog.cards[ComponentCapability.TRANSLATION].download_button
    assert not button.isHidden()
    button.click()

    assert requests == [ComponentCapability.TRANSLATION]
    assert not hasattr(dialog, "custom_model_input")
    assert not hasattr(dialog, "search_input")


def test_component_setup_refresh_is_an_explicit_view_signal(qtbot) -> None:
    dialog = ComponentSetupDialog(states={})
    qtbot.addWidget(dialog)
    refreshes: list[bool] = []
    dialog.refresh_requested.connect(lambda: refreshes.append(True))

    dialog.refresh_button.click()

    assert refreshes == [True]


def test_injected_states_are_replaced_without_accepting_unknown_capabilities(qtbot) -> None:
    dialog = ComponentSetupDialog(states={})
    qtbot.addWidget(dialog)
    dialog.set_readiness(
        {
            "translation": "prepared",
            "visual": ReadinessStatus.DOWNLOADABLE,
        }
    )

    assert dialog.cards[ComponentCapability.TRANSLATION].status_label.text() == "Preparado"
    assert dialog.cards[ComponentCapability.REVIEW].status_label.text() == "Equipo insuficiente"
    assert set(dialog.readiness) == {
        ComponentCapability.TRANSLATION,
        ComponentCapability.REVIEW,
    }


def test_catalog_injection_uses_the_pure_evaluator_without_network_or_download(qtbot) -> None:
    from parsezen.component_catalog import REVIEW_COMPONENT_ENTRY
    from parsezen.local_ai_policy import ComponentVerification

    dialog = ComponentSetupDialog(
        catalog={ComponentCapability.REVIEW: REVIEW_COMPONENT_ENTRY},
        hardware=LocalHardware(
            ram_available_mebibytes=8_192,
            disk_free_bytes=3_000_000_000,
        ),
        verifications={
            ComponentCapability.REVIEW: ComponentVerification(
                False,
                ("model_not_installed",),
            )
        },
        local_only_configured=True,
    )
    qtbot.addWidget(dialog)

    assert dialog.cards[ComponentCapability.REVIEW].status_label.text() == "Descargable"
    assert dialog.cards[ComponentCapability.TRANSLATION].status_label.text() == (
        "Equipo insuficiente"
    )


def test_component_setup_presentation_has_no_model_recommendation_or_manual_tag_flow() -> None:
    source = Path("src/parsezen/presentation/component_setup.py").read_text(encoding="utf-8")

    assert "model_recommendations" not in source
    assert "llmfit" not in source
    assert "QLineEdit" not in source


def test_local_ai_workflow_opens_the_fixed_component_view(qtbot, tmp_path) -> None:
    from parsezen.presentation.component_setup import ComponentSetupDialog
    from parsezen.presentation.main_window import ParsezenMainWindow
    from parsezen.settings import AppSettings

    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    window._local_ai_workflow.show_component_setup()  # noqa: SLF001

    setup = window._local_ai_workflow.component_setup  # noqa: SLF001
    assert isinstance(setup, ComponentSetupDialog)
    assert not hasattr(setup, "custom_model_input")
    assert not hasattr(setup, "search_input")
    setup.reject()


def test_prepared_component_readiness_propagates_verified_manifest_identity(
    qtbot, tmp_path
) -> None:
    from parsezen.component_catalog import TRANSLATION_COMPONENT_MANIFEST
    from parsezen.presentation.main_window import ParsezenMainWindow
    from parsezen.settings import AppSettings

    window = ParsezenMainWindow(
        settings=AppSettings(),
        auto_discover_ai=False,
        state_path=tmp_path / "workspace.sqlite3",
    )
    qtbot.addWidget(window)

    window._local_ai_workflow.set_component_readiness(  # noqa: SLF001
        {
            ComponentCapability.TRANSLATION: ComponentReadiness(
                ComponentCapability.TRANSLATION,
                ReadinessStatus.PREPARED,
            ),
            ComponentCapability.REVIEW: ComponentReadiness(
                ComponentCapability.REVIEW,
                ReadinessStatus.INSUFFICIENT,
            ),
        }
    )

    snapshot = window._local_ai_policy.translation  # noqa: SLF001
    assert snapshot is not None
    assert snapshot.policy_version == TRANSLATION_COMPONENT_MANIFEST.policy_version
    assert snapshot.model == TRANSLATION_COMPONENT_MANIFEST.model_name
    assert snapshot.digest == TRANSLATION_COMPONENT_MANIFEST.ollama_digest
    assert snapshot.context_window == TRANSLATION_COMPONENT_MANIFEST.context_window
    assert window._local_ai_policy.review is None  # noqa: SLF001
