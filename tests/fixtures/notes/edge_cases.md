# Edge cases

Inline code with a fake task: `- [ ] not a real task` and a `[fake](link)`.

```
- [ ] task inside a fenced block (should be ignored)
[code link](https://example.com/ignored)
```

~~~
+ [x] tilde-fenced task ignored
~~~

- [ ] Real task after the fences with a [link](:/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
