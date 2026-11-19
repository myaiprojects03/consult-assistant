// Everything the firm changes to make the widget theirs: name, wording and colours.
// The assistant's own name, greeting and rules live in content/rules.yaml (served by the API).
export const brand = {
  firmName: "Acme IT Consulting",
  launcherLabel: "Chat with us",
  headerSubtitle: "Usually replies in a few seconds",
  inputPlaceholder: "Type your question",
  privacyUrl: "/privacy",
  privacyNote: "Your messages are stored to answer you and arrange a call.",
  colors: {
    ink: "#10282C",        // header background, headings
    accent: "#1F7A6B",     // visitor messages, buttons, launcher
    accentInk: "#FFFFFF",  // text on accent
    surface: "#FFFFFF",    // chat background
    soft: "#EEF4F2",       // assistant messages
    line: "#D7E2DF",       // borders
    muted: "#5B6B6A",      // secondary text
    alert: "#9A3412",      // errors
  },
};
