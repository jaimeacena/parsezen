from PySide6.QtCore import Qt

from parsezen.presentation.components import StatusMessage, Switch


def test_switch_is_keyboard_operable_and_exposes_its_purpose(qtbot) -> None:
    switch = Switch()
    qtbot.addWidget(switch)
    switch.setAccessibleName("Incluir imágenes")
    switch.show()
    switch.setFocus()

    qtbot.keyClick(switch, Qt.Key.Key_Space)

    assert switch.isChecked()
    assert switch.accessibleName() == "Incluir imágenes"
    assert "Espacio" in switch.accessibleDescription()


def test_status_message_combines_tone_text_and_recovery_action(qtbot) -> None:
    message = StatusMessage()
    qtbot.addWidget(message)
    requested: list[bool] = []
    message.actionRequested.connect(lambda: requested.append(True))

    message.show_message(
        "No se pudo procesar el documento.",
        tone="error",
        action_label="Revisar configuración",
    )
    message.action.click()

    assert message.property("tone") == "error"
    assert message.accessibleName() == "No se pudo procesar el documento."
    assert requested == [True]
