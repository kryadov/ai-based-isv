// Business logic of the demo service, kept free of I/O so it is unit-testable.
export function greet(name: string): string {
  const who = name.trim() || "world";
  return `hello, ${who}`;
}
