use futures::{Async, BoxFuture};
use tokio_service::Service;
use tokio_core::reactor::Handle;
use minihttp::request::Request;
use minihttp::{Error, Status};
use tokio_curl::Session;

use config::ConfigCell;
use response::DebugInfo;
use routing::{parse_host, route};
use serializer::{Response, Serializer};
use config::Handler;
use handlers::{files, proxy};
use websocket;

#[derive(Clone)]
pub struct Main {
    pub config: ConfigCell,
    pub handle: Handle,
    pub curl_session: Session,
}

impl Service for Main {
    type Request = Request;
    type Response = Serializer;
    type Error = Error;
    type Future = BoxFuture<Self::Response, Error>;

    fn call(&self, req: Request) -> Self::Future {
        // We must store configuration for specific request for the case
        // it changes in runtime. Config changes in the middle of request
        // can create undesirable effects
        let cfg = self.config.get();
        let mut debug = DebugInfo::new(&req);

        let response = {
            let matched_route = req.host().map(parse_host)
                .and_then(|host| route(host, &req.path, &cfg.routing));
            if let Some((route, suffix)) = matched_route {
                debug.set_route(route);
                match cfg.handlers.get(route) {
                    Some(&Handler::EmptyGif) => {
                        Response::EmptyGif
                    }
                    Some(&Handler::Static(ref settings)) => {
                        if let Ok(path) = files::path(settings, suffix, &req) {
                            Response::Static {
                                path: path,
                                settings: settings.clone(),
                            }
                        } else {
                            Response::ErrorPage(Status::Forbidden)
                        }
                    }
                    Some(&Handler::SingleFile(ref settings)) => {
                        Response::SingleFile(settings.clone())
                    }
                    Some(&Handler::WebsocketEcho) => {
                        match websocket::prepare(&req) {
                            Ok(init) => {
                                Response::WebsocketEcho(init)
                            }
                            Err(status) => {
                                // TODO(tailhook) use real status
                                Response::ErrorPage(status)
                            }
                        }
                    }
                    Some(&Handler::Proxy(ref settings)) => {
                        if let Some(dest) = cfg.http_destinations
                                .get(&settings.destination.upstream)
                        {
                            use handlers::proxy::ProxyCall::*;
                            let hostport = proxy::pick_backend_host(dest);
                            Response::Proxy(Prepare {
                                hostport: hostport,
                                settings: settings.clone(),
                                session: self.curl_session.clone(),
                            })
                        } else {
                            Response::ErrorPage(Status::NotFound)
                        }
                    }
                    // TODO(tailhook) make better error code for None
                    _ => {
                        Response::ErrorPage(Status::NotImplemented)
                    }
                }
            } else {
                Response::ErrorPage(Status::NotFound)
            }
        };
        response.serve(req, cfg.clone(), debug, &self.handle)
    }

    fn poll_ready(&self) -> Async<()> {
        Async::Ready(())
    }
}
