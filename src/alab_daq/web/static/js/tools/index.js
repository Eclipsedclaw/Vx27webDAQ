// The tools of the Tools page, in the order they are shown. Each is a module whose default export is
//   { id, title, summary (texts marked with T so that they are translated), icon, open: true|false (the first time),
//     badge(app) (optional: a short text for the closed section's title bar, from app.state),
//     make(body, ctx) -> { update(type, msg), unmount() } (both optional) }
// make() runs the first time the section is opened; body is the section's element. ctx has app (the shared
// state, app.can("control"), ...), open(id) to open another tool, and on(name, fn) / emit(name, data) for the
// tools to tell each other things ("overview-file" when a file's overview is wanted).
// A new tool: write js/tools/NAME.js, import it here, put it in the list, and add its texts to i18n/zh.json.
import boards from "./boards.js";
import datafile from "./datafile.js";
import overview from "./overview.js";
import rootconv from "./rootconv.js";

export const TOOLS = [overview, datafile, boards, rootconv];
