use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};

#[derive(Debug, Default)]
pub struct DiagnosticState {
    pub backend_version: String,
    pub protocol_version: u32,
    pub event_schema_version: u32,
    pub event_count: u64,
    pub last_event: Option<String>,
    pub status: &'static str,
}

pub fn render(frame: &mut Frame<'_>, state: &DiagnosticState) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(5),
            Constraint::Length(3),
        ])
        .split(frame.area());
    let title = Paragraph::new("WISP / NATIVE TRANSPORT")
        .alignment(Alignment::Center)
        .style(
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        )
        .block(Block::default().borders(Borders::ALL));
    frame.render_widget(title, chunks[0]);

    let last_event = state.last_event.as_deref().unwrap_or("none");
    let body = Text::from(vec![
        Line::from(format!("status       {}", state.status)),
        Line::from(format!("backend      {}", state.backend_version)),
        Line::from(format!(
            "contract     rpc v{} / events v{}",
            state.protocol_version, state.event_schema_version
        )),
        Line::from(format!("events       {}", state.event_count)),
        Line::from(format!("last event   {last_event}")),
    ]);
    frame.render_widget(
        Paragraph::new(body)
            .block(Block::default().title(" diagnostic ").borders(Borders::ALL))
            .wrap(Wrap { trim: true }),
        chunks[1],
    );
    frame.render_widget(
        Paragraph::new("q or Ctrl-C: shut down")
            .alignment(Alignment::Center)
            .style(Style::default().fg(Color::DarkGray))
            .block(Block::default().borders(Borders::ALL)),
        chunks[2],
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    #[test]
    fn diagnostic_screen_contains_negotiated_contract() {
        let backend = TestBackend::new(60, 14);
        let mut terminal = Terminal::new(backend).unwrap();
        let state = DiagnosticState {
            backend_version: "0.9.0".into(),
            protocol_version: 2,
            event_schema_version: 34,
            event_count: 7,
            last_event: Some("rpc.state".into()),
            status: "connected",
        };
        terminal.draw(|frame| render(frame, &state)).unwrap();
        let rendered = terminal.backend().to_string();
        assert!(rendered.contains("rpc v2 / events v34"));
        assert!(rendered.contains("rpc.state"));
        assert!(rendered.contains("q or Ctrl-C"));
    }
}
