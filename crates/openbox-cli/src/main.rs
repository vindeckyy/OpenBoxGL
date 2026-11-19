mod library_cmd;

fn main() {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if library_cmd::is_library_command(&argv) {
        std::process::exit(library_cmd::run(&argv, None));
    }
    println!("openbox-server bootstrap");
}
