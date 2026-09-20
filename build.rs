use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=TOOLFRAME_BUNDLE_DIR");
    let mut contents = String::from("pub const WHEELS: &[(&str, &[u8])] = &[\n");
    if let Some(directory) = env::var_os("TOOLFRAME_BUNDLE_DIR") {
        let directory = PathBuf::from(directory)
            .canonicalize()
            .expect("bundle directory");
        println!("cargo:rerun-if-changed={}", directory.display());
        let mut paths: Vec<_> = fs::read_dir(&directory)
            .expect("read bundle")
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|extension| extension == "whl"))
            .collect();
        paths.sort();
        for path in paths {
            let name = path.file_name().unwrap().to_str().unwrap();
            println!("cargo:rerun-if-changed={}", path.display());
            contents.push_str(&format!(
                "({name:?}, include_bytes!({:?})),\n",
                path.to_str().unwrap()
            ));
        }
    }
    contents.push_str("];\n");
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("bundle.rs"),
        contents,
    )
    .unwrap();
}
