//! Gama backend-evaluation prototype (Rust + lofty). Disposable research code.
//!
//! proto read <paths...>
//! proto set-genres --genres "A;B" --backup-dir D --journal J [--continue-on-error] [--fail-after N] <files...>
//! proto set-fields [--backup-dir D] [--journal J] [--continue-on-error] [--fail-after N] <files...>
//! proto rollback --journal J
//! proto direct-save <file>   (diagnostic: lofty's own in-place save_to_path, no protocol)

mod fmt;
mod safe;

use serde_json::{Value, json};
use std::path::PathBuf;
use std::process::ExitCode;

fn usage() -> ExitCode {
    eprintln!(
        "usage: proto read <paths...> | set-genres --genres A;B --backup-dir D --journal J [--continue-on-error] [--fail-after N] <files...> | set-fields [...] <files...> | rollback --journal J"
    );
    ExitCode::from(2)
}

pub fn print(v: &Value) {
    println!("{}", serde_json::to_string_pretty(v).unwrap());
}

fn main() -> ExitCode {
    // Keep panics from printing to stderr as noise; they are caught and reported as JSON.
    std::panic::set_hook(Box::new(|_| {}));
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first() else {
        return usage();
    };
    let rest = &args[1..];
    let code = match cmd.as_str() {
        "read" => fmt::cmd_read(rest),
        "set-genres" | "set-fields" => match safe::WriteArgs::parse(cmd, rest) {
            Ok(a) => safe::cmd_write(a),
            Err(e) => {
                print(&json!({"ok": false, "error": e}));
                2
            }
        },
        "rollback" => match rest {
            [flag, j] if flag == "--journal" => safe::cmd_rollback(&PathBuf::from(j)),
            _ => return usage(),
        },
        "direct-save" => fmt::cmd_direct_save(rest),
        _ => return usage(),
    };
    // Exit explicitly: a timed-out worker thread may still be running.
    std::process::exit(code)
}
