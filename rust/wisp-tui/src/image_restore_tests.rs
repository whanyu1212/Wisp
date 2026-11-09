//! Restoring a message's images to the composer: queue pop, a rejected follow-up,
//! fork-to-edit, tree navigation, and leaving the session whose store holds them.

use super::*;
use composer_images::AttachedImage;
use reducer::{ActiveCommand, ActiveCommandType, RestoredDraft};
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, DuplexStream, duplex};

fn part(sha: char) -> Value {
    json!({
        "media_type": "image/jpeg",
        "sha256": sha.to_string().repeat(64),
        "byte_size": 2048,
        "width": 640,
        "height": 480,
    })
}

fn stored(sha: char) -> ComposerImages {
    std::iter::once(AttachedImage::stored(&part(sha)).unwrap()).collect()
}

fn pasted() -> AttachedImage {
    AttachedImage::from_rgba(1, 1, &[0, 0, 0, 255]).unwrap()
}

fn running_ui() -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.view_status = ViewStatus::Running;
    ui.state.interaction_status = InteractionStatus::Running;
    ui.state.current_command = Some(ActiveCommand {
        id: "prompt-1".into(),
        command_type: ActiveCommandType::Prompt,
    });
    ui
}

struct Wire {
    tx: mpsc::Sender<WriterMessage>,
    server: DuplexStream,
    task: tokio::task::JoinHandle<Result<(), Error>>,
}

impl Wire {
    fn new() -> Self {
        let (client, server) = duplex(64 * 1024);
        let (tx, rx) = mpsc::channel(WRITER_CHANNEL_CAPACITY);
        Self {
            tx,
            server,
            task: tokio::spawn(writer_task(client, rx)),
        }
    }

    async fn next_frame(&mut self) -> Value {
        let mut frame = vec![0; 64 * 1024];
        let len = self.server.read(&mut frame).await.unwrap();
        serde_json::from_slice(&frame[..len - 1]).unwrap()
    }

    async fn close(self) {
        drop(self.tx);
        self.task.await.unwrap().unwrap();
    }
}

async fn deliver(ui: &mut LiveUi, wire: &Wire, event: Value) {
    let event = BackendEvent::from_projection_value(&event).unwrap();
    ui.dispatch(
        UiAction::BackendEvent(event),
        &wire.tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
}

async fn press(ui: &mut LiveUi, wire: &Wire, code: KeyCode, modifiers: KeyModifiers) {
    ui.handle_input(
        Input::Key(KeyEvent::new(code, modifiers)),
        &wire.tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
}

/// Pop the newest follow-up with Alt+Up and deliver the backend's removal of it.
async fn pop_follow_up(ui: &mut LiveUi, wire: &mut Wire, content: &str, images: Value) {
    press(ui, wire, KeyCode::Up, KeyModifiers::ALT).await;
    assert_eq!(wire.next_frame().await["type"], "pop_queue");
    deliver(
        ui,
        wire,
        json!({
            "type": "queue.items.removed", "command_id": "pop_queue-1",
            "operation": "pop", "kind": "follow_up",
            "steering": [], "follow_up": [content], "follow_up_images": [images],
        }),
    )
    .await;
    deliver(
        ui,
        wire,
        json!({"type": "rpc.command.finished", "command_id": "pop_queue-1",
               "command_type": "pop_queue", "ok": true}),
    )
    .await;
}

#[tokio::test]
async fn popped_message_restores_images() {
    let mut wire = Wire::new();
    let mut ui = running_ui();
    deliver(
        &mut ui,
        &wire,
        json!({"type": "queue.updated", "steering": [], "follow_up": [""],
               "follow_up_images": [[part('a')]], "token": "queue-1"}),
    )
    .await;
    assert_eq!(
        ui.state.queue.follow_up[0].image_note().as_deref(),
        Some("[1 image]")
    );

    pop_follow_up(&mut ui, &mut wire, "", json!([part('a')])).await;

    assert_eq!(ui.images.chip_labels(), ["Image 1 · 640×480"]);
    assert_eq!(ui.editor.text(), "");
    assert_eq!(ui.state.queued_follow_ups(), 0);
    // Re-sending sends the stored image by reference, not as bytes.
    press(&mut ui, &wire, KeyCode::Enter, KeyModifiers::NONE).await;
    let sent = wire.next_frame().await;
    assert_eq!(sent["type"], "follow_up");
    assert_eq!(sent["images"], json!([part('a')]));
    assert!(ui.images.is_empty());
    wire.close().await;
}

#[tokio::test]
async fn overflowing_pop_is_refused() {
    let mut wire = Wire::new();
    let mut ui = running_ui();
    for _ in 0..composer_images::MAX_IMAGES {
        ui.images.attach(pasted(), None).unwrap();
    }
    deliver(
        &mut ui,
        &wire,
        json!({"type": "queue.updated", "steering": [], "follow_up": ["look"],
               "follow_up_images": [[part('a')]], "token": "queue-1"}),
    )
    .await;

    // The images would not fit, so the message stays queued rather than popped.
    press(&mut ui, &wire, KeyCode::Up, KeyModifiers::ALT).await;
    assert!(
        ui.notice
            .as_deref()
            .unwrap()
            .contains("kept the queue unchanged")
    );
    assert_eq!(ui.state.queued_follow_ups(), 1);

    ui.images.remove_last();
    pop_follow_up(&mut ui, &mut wire, "look", json!([part('a')])).await;
    assert_eq!(ui.editor.text(), "look");
    assert_eq!(ui.images.chip_labels()[0], "Image 1 · 640×480");
    assert_eq!(ui.images.len(), composer_images::MAX_IMAGES);
    wire.close().await;
}

#[tokio::test]
async fn rejected_follow_up_keeps_images() {
    let mut wire = Wire::new();
    let mut ui = running_ui();
    ui.images.attach(pasted(), None).unwrap();
    let encoded = ui.images.wire_images();
    press(&mut ui, &wire, KeyCode::Enter, KeyModifiers::NONE).await;
    let sent = wire.next_frame().await;
    assert_eq!(sent["content"], "");
    assert_eq!(sent["images"], json!(encoded));
    assert!(ui.images.is_empty());

    deliver(
        &mut ui,
        &wire,
        json!({"type": "rpc.command.finished", "command_id": sent["id"],
               "command_type": "follow_up", "ok": false, "error": "refused"}),
    )
    .await;
    // An image-only message waits until its images fit, then comes back whole.
    for _ in 0..composer_images::MAX_IMAGES {
        ui.images.attach(pasted(), None).unwrap();
    }
    press(&mut ui, &wire, KeyCode::Up, KeyModifiers::ALT).await;
    assert!(ui.notice.as_deref().unwrap().contains("remove some"));
    assert_eq!(ui.deferred_queue_recovery.len(), 1);
    ui.images.clear();
    press(&mut ui, &wire, KeyCode::Up, KeyModifiers::ALT).await;

    assert_eq!(ui.editor.text(), "");
    assert_eq!(ui.images.wire_images(), encoded);
    assert!(ui.deferred_queue_recovery.is_empty());
    wire.close().await;
}

#[test]
fn forked_prompt_projects_images() {
    let forked = BackendEvent::from_projection_value(&json!({
        "type": "rpc.session.forked", "command_id": "fork_session-1",
        "source_session_id": "source", "source_session_path": "/s/source.jsonl",
        "session_id": "fork", "session_path": "/s/fork.jsonl", "entry_count": 2,
        "selected_entry_id": "user-2", "selected_prompt": "",
        "selected_images": [part('a'), part('b')],
    }))
    .unwrap();
    let BackendEvent::SessionForked { derived, .. } = forked else {
        panic!("expected a fork: {forked:?}")
    };
    assert_eq!(
        derived.selected_images.wire_images(),
        [part('a'), part('b')]
    );

    let navigated = BackendEvent::from_projection_value(&json!({
        "type": "rpc.session.tree.navigated", "command_id": "navigate_session_tree-1",
        "session_id": "s", "session_path": "/s/s.jsonl", "selected_entry_id": "user-2",
        "previous_active_leaf_id": "leaf", "active_leaf_id": "user-1",
        "editor_text": "look", "editor_images": [part('c')],
        "changed": true, "entry_count": 4,
    }))
    .unwrap();
    let BackendEvent::SessionTreeNavigated { navigation, .. } = navigated else {
        panic!("expected a navigation: {navigated:?}")
    };
    assert_eq!(navigation.editor_images, stored('c'));
}

#[test]
fn queue_images_must_align() {
    let updated = |images: Value| {
        BackendEvent::from_projection_value(&json!({
            "type": "queue.updated", "steering": ["a", "b"], "follow_up": [],
            "steering_images": images,
        }))
    };

    assert!(updated(json!([[part('a')]])).is_err());
    assert!(updated(json!([[], [], [part('a')]])).is_err());
    let Ok(BackendEvent::QueueUpdated {
        steering_image_counts,
        ..
    }) = updated(json!([]))
    else {
        panic!("an empty images array is valid")
    };
    assert!(steering_image_counts.is_empty());
    let mut short_digest = part('a');
    short_digest["sha256"] = "a".repeat(63).into();
    assert!(updated(json!([[], [short_digest]])).is_err());
    let Ok(BackendEvent::QueueUpdated {
        steering_image_counts,
        ..
    }) = updated(json!([[], [part('a'), part('b')]]))
    else {
        panic!("aligned images are valid")
    };
    assert_eq!(steering_image_counts, [0, 2]);
}

#[tokio::test]
async fn session_draft_adds_images() {
    let wire = Wire::new();
    let mut ui = LiveUi::default();
    ui.images.attach(pasted(), None).unwrap();

    ui.apply_effects(
        vec![UiEffect::RestoreSessionDraft(RestoredDraft {
            text: "edit me".into(),
            images: stored('a'),
        })],
        &wire.tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();

    assert_eq!(ui.editor.text(), "edit me");
    assert_eq!(ui.images.wire_images()[0], part('a'));
    assert_eq!(ui.images.len(), 2);
    wire.close().await;
}

#[tokio::test]
async fn leaving_store_drops_references() {
    let wire = Wire::new();
    let mut ui = LiveUi::default();
    ui.images.attach(pasted(), None).unwrap();
    ui.images.prepend_restored(&stored('a')).unwrap();
    for (content, images) in [("", stored('b')), ("keep me", stored('c'))] {
        ui.defer_queue_recovery(content.into(), None, images);
    }

    ui.apply_effects(
        vec![UiEffect::DropStoredImages],
        &wire.tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();

    assert_eq!(ui.images.len(), 1);
    assert!(ui.images.wire_images()[0].is_string());
    // The image-only recovery had nothing left; the other keeps its text.
    assert_eq!(ui.deferred_queue_recovery.len(), 1);
    assert_eq!(ui.deferred_queue_recovery[0].content, "keep me");
    assert!(ui.deferred_queue_recovery[0].images.is_empty());
    assert_eq!(
        ui.notice.as_deref(),
        Some("Removed 3 restored image(s): they are stored with the previous session.")
    );
    wire.close().await;
}
