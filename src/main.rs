extern crate futures;
extern crate tokio_core;
extern crate tokio_service;

extern crate minihttp;

use std::io;
use futures::{Async, Finished};
use tokio_core::reactor::Core;
use minihttp::server::Message;
use minihttp::request::Request;
use minihttp::response::Response;


#[derive(Clone)]
struct HelloWorld;

impl minihttp::server::HttpService for HelloWorld {
    type Request = Request;
    type Response = Response;
    type Error = io::Error;
    type Future = Finished<Message<Response>, io::Error>;

    fn call(&self, req: Request) -> Self::Future {
        println!("REQUEST: {:?}", req);
        let resp = req.new_response();
        // resp.header("Content-Type", "text/html");
        // resp.body(
        //     format!("<h4>Hello world</h4>\n<b>Method: {:?}</b>",
        //             _request.method).as_str());
        // let resp = resp;

        futures::finished(Message::WithoutBody(resp))
    }

    fn poll_ready(&self) -> Async<()> {
        Async::Ready(())
    }
}

pub fn main() {
    let mut lp = Core::new().unwrap();

    let addr = "0.0.0.0:8080".parse().unwrap();
    minihttp::serve(&lp.handle(), addr, || HelloWorld);
    lp.run(futures::empty::<(), ()>()).unwrap();
}
