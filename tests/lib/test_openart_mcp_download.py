"""MCP dynamic CDN redirects retain the existing pinned public HTTPS boundary."""
import socket
import pytest
from lib import openart_download as download
from tests.lib.test_openart_download import transport, Response


def collect(tmp_path,url):
    from urllib.parse import urlsplit
    return download.collect_output(url,tmp_path/'original.mp4',allowed_hosts={urlsplit(url).hostname},
        output_root=tmp_path,allow_public_redirects=True,retain_private_trace=True)


@pytest.mark.parametrize('url',['https://127.0.0.1/video','https://user:secret@cdn.example/video','http://cdn.example/video'])
def test_unsafe_initial_never_connects(tmp_path,transport,url):
    with pytest.raises(download.OpenArtDownloadError):collect(tmp_path,url)
    assert transport[0]==[]


def test_private_initial_dns_never_connects(tmp_path,transport,monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('10.0.0.1',443))])
    with pytest.raises(download.OpenArtDownloadError):collect(tmp_path,'https://private.example/video')
    assert transport[0]==[]


@pytest.mark.parametrize('location',['https://private.example/video','http://public-next.example/video','https://user:secret@public-next.example/video'])
def test_unsafe_redirect_never_connects_target(tmp_path,transport,monkeypatch,location):
    def dns(host,port,**kw):
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('10.0.0.1' if host=='private.example' else '8.8.8.8',port))]
    monkeypatch.setattr(socket,'getaddrinfo',dns)
    transport[1].append(Response(status=302,headers={'Location':location}))
    with pytest.raises(download.OpenArtDownloadError):collect(tmp_path,'https://cdn.example/video')
    assert [c['host'] for c in transport[0]]==['cdn.example']


def test_new_public_cdn_redirect_is_validated_then_downloaded(tmp_path,transport):
    transport[1].extend([Response(status=302,headers={'Location':'https://new-public-cdn.example/video?signature=private'}),Response()])
    result=collect(tmp_path,'https://cdn.example/video')
    assert [c['host'] for c in transport[0]]==['cdn.example','new-public-cdn.example']
    assert result['final_url']=='https://new-public-cdn.example/video?signature=private'
    assert len(result['redirect_chain'])==2
